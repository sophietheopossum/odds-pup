"""Connection setup, transactions and migrations. SPEC §11.1, §11.3."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import TYPE_CHECKING

from odds_pup.storage.errors import NewerDatabaseError
from odds_pup.storage.paths import FILE_MODE
from odds_pup.storage.schema import MIGRATIONS, SCHEMA_VERSION

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

BUSY_TIMEOUT_MS = 5000


def connect(path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the ledger database with the SPEC §11.1 settings.

    Autocommit mode with explicit ``BEGIN IMMEDIATE``/``COMMIT`` via :func:`transaction`; no
    type adapters, so timestamps and odds are plain text in both directions.
    """
    existed = path.exists()
    connection = sqlite3.connect(path, isolation_level=None, detect_types=0)
    if not existed:
        os.chmod(path, FILE_MODE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One user action, one transaction (SPEC §11.1)."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def migrate(
    connection: sqlite3.Connection,
    *,
    before_migration: Callable[[], None] | None = None,
) -> int:
    """Bring the database up to :data:`SCHEMA_VERSION`; refuse one that is newer.

    ``before_migration`` runs once, before the first migration, when an existing database is
    about to change (the caller takes a backup there). A brand-new empty file gets no backup.
    """
    current = schema_version(connection)
    if current > SCHEMA_VERSION:
        raise NewerDatabaseError(
            f"this ledger was written by a newer odds-pup (schema {current}; "
            f"this version understands {SCHEMA_VERSION}). Upgrade odds-pup rather than open it."
        )
    if current == SCHEMA_VERSION:
        return current
    if current > 0 and before_migration is not None:
        before_migration()
    for version, script in MIGRATIONS:
        if version <= current:
            continue
        connection.executescript(f"BEGIN;\n{script}\nCOMMIT;")
        if schema_version(connection) != version:  # pragma: no cover - script bug guard
            raise RuntimeError(f"migration {version} did not set user_version")
    return SCHEMA_VERSION
