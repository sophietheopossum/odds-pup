"""Connection setup, transactions and migrations. SPEC §11.1, §11.3."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING

from odds_pup.storage.errors import DatabaseBusyError, MigrationError, NewerDatabaseError, translate
from odds_pup.storage.paths import FILE_MODE
from odds_pup.storage.schema import MIGRATIONS, SCHEMA_VERSION

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

BUSY_TIMEOUT_MS = 5000
_SIDECARS = ("-wal", "-shm", "-journal")


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the ledger database with the SPEC §11.1 per-connection settings.

    Nothing here writes to the file: the journal mode switch and migrations happen only after
    :func:`check_version` has confirmed this app understands the schema. Autocommit mode with
    explicit ``BEGIN IMMEDIATE``/``COMMIT`` via :func:`transaction`; no type adapters.
    """
    try:
        connection = sqlite3.connect(path, isolation_level=None, detect_types=0)
    except sqlite3.Error as exc:
        raise translate(exc) from exc
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    except BaseException as exc:
        connection.close()
        if isinstance(exc, sqlite3.Error):
            raise translate(exc) from exc
        raise
    return connection


def tighten_permissions(path: Path) -> None:
    """Make the database and any sidecar files owner-only (SPEC §11.4).

    Done before WAL is enabled so SQLite creates ``-wal``/``-shm`` with the tight mode (it
    copies the main file's permissions).
    """
    for candidate in (path, *(path.with_name(path.name + s) for s in _SIDECARS)):
        if candidate.exists():
            os.chmod(candidate, FILE_MODE)


def schema_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error as exc:
        raise translate(exc) from exc
    return int(row[0])


def check_version(connection: sqlite3.Connection) -> int:
    """Refuse a database written by a newer odds-pup before anything writes to it (§11.3)."""
    current = schema_version(connection)
    if current > SCHEMA_VERSION:
        raise NewerDatabaseError(
            f"this ledger was written by a newer odds-pup (schema {current}; "
            f"this version understands {SCHEMA_VERSION}). Upgrade odds-pup rather than open it."
        )
    return current


def enable_wal(connection: sqlite3.Connection) -> None:
    """Switch to WAL once, waiting up to the busy timeout if another program holds a lock."""
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if mode == "wal":
                return
            mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
            if mode == "wal":
                return
        except sqlite3.OperationalError as exc:
            if time.monotonic() >= deadline:
                raise translate(exc) from exc
        if time.monotonic() >= deadline:
            raise DatabaseBusyError("could not switch the ledger to WAL mode: it is locked")
        time.sleep(0.05)


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    """Roll back if a transaction is still open; never let this mask the original error.

    SQLite rolls back by itself on SQLITE_FULL, SQLITE_IOERR and similar, in which case an
    explicit ROLLBACK would raise "no transaction is active".
    """
    if connection.in_transaction:
        with suppress(sqlite3.Error):
            connection.execute("ROLLBACK")


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One user action, one transaction (SPEC §11.1). Raw sqlite3 errors become StorageErrors."""
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise translate(exc) from exc
    try:
        yield connection
    except BaseException as exc:
        _rollback_quietly(connection)
        if isinstance(exc, sqlite3.Error):
            raise translate(exc) from exc
        raise
    try:
        connection.execute("COMMIT")
    except sqlite3.Error as exc:
        _rollback_quietly(connection)
        raise translate(exc) from exc


@contextmanager
def translated() -> Iterator[None]:
    """For reads outside a transaction: surface sqlite3 errors as StorageErrors."""
    try:
        yield
    except sqlite3.Error as exc:
        raise translate(exc) from exc


def migrate(
    connection: sqlite3.Connection,
    *,
    before_migration: Callable[[], None] | None = None,
) -> int:
    """Bring the database up to :data:`SCHEMA_VERSION`; refuse one that is newer.

    ``before_migration`` runs once, before the first migration, when an existing database is
    about to change (the caller takes a backup there). A brand-new empty file gets no backup.
    Each migration runs in its own transaction; a failure rolls it back and raises
    :class:`MigrationError`, leaving the connection usable.
    """
    current = check_version(connection)
    if current == SCHEMA_VERSION:
        return current
    if current > 0 and before_migration is not None:
        before_migration()
    for version, script in MIGRATIONS:
        if version <= current:
            continue
        try:
            connection.executescript(f"BEGIN IMMEDIATE;\n{script}\nCOMMIT;")
        except sqlite3.Error as exc:
            _rollback_quietly(connection)
            raise MigrationError(
                f"schema migration {version} failed and was rolled back: {exc}"
            ) from exc
        if schema_version(connection) != version:  # pragma: no cover - script bug guard
            raise MigrationError(f"migration {version} did not set user_version")
    return SCHEMA_VERSION
