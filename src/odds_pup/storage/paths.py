"""Where the ledger lives on disk. SPEC §11.4."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import platformdirs

if TYPE_CHECKING:
    from collections.abc import Mapping

APP_NAME = "odds-pup"
ENV_VAR = "ODDS_PUP_DATA_DIR"
DATABASE_FILE = "odds-pup.sqlite3"
BACKUPS_DIR = "backups"
LOCK_FILE = "odds-pup.lock"
DIR_MODE = 0o700
FILE_MODE = 0o600


@dataclass(frozen=True, slots=True)
class DataPaths:
    root: Path

    @property
    def database(self) -> Path:
        return self.root / DATABASE_FILE

    @property
    def backups(self) -> Path:
        return self.root / BACKUPS_DIR

    @property
    def lock(self) -> Path:
        return self.root / LOCK_FILE


def resolve_data_dir(
    explicit: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """``--data-dir`` beats ``$ODDS_PUP_DATA_DIR`` beats the platform user data directory."""
    environment = os.environ if env is None else env
    if explicit:
        return Path(explicit).expanduser()
    if environment.get(ENV_VAR):
        return Path(environment[ENV_VAR]).expanduser()
    return Path(platformdirs.user_data_dir(APP_NAME))


def prepare(paths: DataPaths) -> DataPaths:
    """Create the data and backup directories owner-only (0700)."""
    for directory in (paths.root, paths.backups):
        directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        os.chmod(directory, DIR_MODE)
    return paths
