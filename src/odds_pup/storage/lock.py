"""Single-instance lock on the data directory. SPEC §11.6 (Linux ``flock``)."""

from __future__ import annotations

import fcntl
import os
from typing import TYPE_CHECKING

from odds_pup.storage.errors import AlreadyRunningError
from odds_pup.storage.paths import FILE_MODE

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType


class InstanceLock:
    """Hold ``odds-pup.lock`` for the life of the process; a second holder fails fast."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise AlreadyRunningError(
                f"the ledger in {self.path.parent} is already open in another odds-pup"
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
