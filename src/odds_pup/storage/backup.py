"""Rotating ``VACUUM INTO`` backups. SPEC §11.5."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

from odds_pup.storage.paths import DIR_MODE, FILE_MODE

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime
    from pathlib import Path

BACKUP_KEEP = 10
BACKUP_PREFIX = "odds-pup-"
BACKUP_SUFFIX = ".sqlite3"
_BACKUP_NAME = re.compile(r"^odds-pup-(\d{8}-\d{6})(?:-(\d+))?\.sqlite3$", re.ASCII)


def _order(path: Path) -> tuple[str, int]:
    match = _BACKUP_NAME.match(path.name)
    assert match is not None  # list_backups only yields matching names
    return match[1], int(match[2] or 0)


def list_backups(backups_dir: Path) -> list[Path]:
    """Existing backups, oldest first (by timestamp, then same-second serial)."""
    if not backups_dir.is_dir():
        return []
    found = [p for p in backups_dir.iterdir() if p.is_file() and _BACKUP_NAME.match(p.name)]
    return sorted(found, key=_order)


def _fsync_path(path: Path, flags: int) -> None:
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_backup(
    connection: sqlite3.Connection,
    backups_dir: Path,
    *,
    now: datetime,
    keep: int = BACKUP_KEEP,
) -> Path:
    """Write a consistent copy of the database, make it durable, then prune to ``keep`` files.

    ``VACUUM INTO`` does not fsync its output, so the copy and the directory entry are synced
    here before any older backup is deleted: a power cut must never leave fewer good backups.
    """
    if keep < 1:
        raise ValueError("keep must be at least 1")
    backups_dir.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    target = backups_dir / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    serial = 1
    while target.exists():  # two backups in one second
        target = backups_dir / f"{BACKUP_PREFIX}{stamp}-{serial}{BACKUP_SUFFIX}"
        serial += 1
    connection.execute("VACUUM INTO ?", (str(target),))
    os.chmod(target, FILE_MODE)
    _fsync_path(target, os.O_RDONLY)
    _fsync_path(backups_dir, os.O_RDONLY | os.O_DIRECTORY)
    existing = list_backups(backups_dir)
    for path in existing:
        os.chmod(path, FILE_MODE)
    for stale in existing[:-keep]:
        stale.unlink()
    return target
