"""Single-instance lock on the data directory. SPEC §11.6 (Linux ``flock``)."""

from __future__ import annotations

import fcntl
import os
from typing import TYPE_CHECKING

from odds_pup.storage.errors import AlreadyRunningError
from odds_pup.storage.paths import DIR_MODE, FILE_MODE

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType


class InstanceLock:
    """Hold ``odds-pup.lock`` for the life of the process; a second holder fails fast.

    ``flock`` locks belong to the open file description, so a second InstanceLock in the same
    process conflicts too, and the lock is released by the kernel if the process dies.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, FILE_MODE)
        try:
            os.fchmod(fd, FILE_MODE)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise AlreadyRunningError(
                f"the ledger in {self.path.parent} is already open in another odds-pup"
            ) from exc
        except BaseException:
            os.close(fd)
            raise
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
