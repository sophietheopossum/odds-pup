"""Rotating ``VACUUM INTO`` backups. SPEC §11.5."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from odds_pup.storage.paths import DIR_MODE, FILE_MODE

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime
    from pathlib import Path

BACKUP_KEEP = 10
BACKUP_PREFIX = "odds-pup-"
BACKUP_SUFFIX = ".sqlite3"


def list_backups(backups_dir: Path) -> list[Path]:
    """Existing backups, oldest first (names sort chronologically)."""
    if not backups_dir.exists():
        return []
    return sorted(
        p
        for p in backups_dir.iterdir()
        if p.is_file() and p.name.startswith(BACKUP_PREFIX) and p.name.endswith(BACKUP_SUFFIX)
    )


def create_backup(
    connection: sqlite3.Connection,
    backups_dir: Path,
    *,
    now: datetime,
    keep: int = BACKUP_KEEP,
) -> Path:
    """Write a consistent copy of the database and prune to the newest ``keep`` files."""
    backups_dir.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    target = backups_dir / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    serial = 1
    while target.exists():  # two backups in one second
        target = backups_dir / f"{BACKUP_PREFIX}{stamp}-{serial}{BACKUP_SUFFIX}"
        serial += 1
    connection.execute("VACUUM INTO ?", (str(target),))
    os.chmod(target, FILE_MODE)
    for stale in list_backups(backups_dir)[:-keep] if keep > 0 else []:
        stale.unlink()
    return target
