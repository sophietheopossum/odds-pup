"""Errors raised by the storage layer, and translation of raw sqlite3 errors into them."""

from __future__ import annotations

import sqlite3


class StorageError(Exception):
    """Base of every error raised by :mod:`odds_pup.storage` other than bad input.

    Bad input raises :class:`odds_pup.core.CoreError` subclasses instead, so a caller that
    catches ``CoreError`` and ``StorageError`` has handled every expected failure.
    """


class NotFoundError(StorageError):
    """The requested row does not exist."""


class IllegalActionError(StorageError):
    """The action is not legal for the bet's current state (SPEC §9.2)."""


class NewerDatabaseError(StorageError):
    """The database was written by a newer odds-pup; refuse to touch it (SPEC §11.3)."""


class AlreadyRunningError(StorageError):
    """Another odds-pup holds the single-instance lock on this data directory (SPEC §11.6)."""


class DatabaseBusyError(StorageError):
    """Another program held the database lock for longer than the busy timeout."""


class DiskFullError(StorageError):
    """SQLite could not write because the disk (or the database's size limit) is full."""


class CorruptDatabaseError(StorageError):
    """The file is not a readable SQLite database."""


class MigrationError(StorageError):
    """A schema migration failed and was rolled back."""


_BUSY = frozenset({"SQLITE_BUSY", "SQLITE_LOCKED"})
_FULL = frozenset({"SQLITE_FULL"})
_CORRUPT = frozenset({"SQLITE_CORRUPT", "SQLITE_NOTADB"})


def translate(exc: sqlite3.Error) -> StorageError:
    """Map a raw sqlite3 error onto the storage error a user can act on."""
    name = (getattr(exc, "sqlite_errorname", "") or "").split("_SNAPSHOT")[0]
    base = name if name in _BUSY | _FULL | _CORRUPT else name.rsplit("_", 1)[0] if name else ""
    message = str(exc)
    lowered = message.lower()
    if isinstance(exc, sqlite3.IntegrityError):
        return StorageError(f"could not save: {message}")
    if base in _BUSY or name in _BUSY or "locked" in lowered or "busy" in lowered:
        return DatabaseBusyError(
            f"the ledger is locked by another program ({message}); close it and try again"
        )
    if base in _FULL or name in _FULL or "full" in lowered:
        return DiskFullError(f"cannot write the ledger: {message}")
    if (
        base in _CORRUPT
        or name in _CORRUPT
        or "not a database" in lowered
        or "malformed" in lowered
    ):
        return CorruptDatabaseError(f"the ledger file is damaged or not a database: {message}")
    return StorageError(message)
