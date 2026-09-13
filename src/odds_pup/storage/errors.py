"""Errors raised by the storage layer."""

from __future__ import annotations


class StorageError(Exception):
    """Base of every error raised by :mod:`odds_pup.storage`."""


class NotFoundError(StorageError):
    """The requested row does not exist (or is not visible)."""


class IllegalActionError(StorageError):
    """The action is not legal for the bet's current state (SPEC §9.2)."""


class NewerDatabaseError(StorageError):
    """The database was written by a newer odds-pup; refuse to touch it (SPEC §11.3)."""


class AlreadyRunningError(StorageError):
    """Another odds-pup holds the single-instance lock on this data directory (SPEC §11.6)."""
