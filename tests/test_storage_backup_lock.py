from __future__ import annotations

import sqlite3
import stat

import pytest

from odds_pup.storage import (
    BACKUP_KEEP,
    AlreadyRunningError,
    DataPaths,
    InstanceLock,
    Repository,
    create_backup,
    list_backups,
)
from tests.storage_helpers import FakeClock, f1_bet


def test_backup_is_a_readable_copy_with_owner_only_permissions(repo: Repository, clock: FakeClock):
    bet = repo.create_bet(f1_bet())
    target = repo.backup()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.name == f"odds-pup-{clock.now:%Y%m%d-%H%M%S}.sqlite3"
    copy = sqlite3.connect(target)
    try:
        assert copy.execute("SELECT COUNT(*) FROM bets WHERE id = ?", (bet.id,)).fetchone()[0] == 1
        assert copy.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        copy.close()


def test_backups_rotate_and_same_second_gets_a_serial(repo: Repository, clock: FakeClock):
    assert repo.paths is not None
    first = create_backup(repo._conn, repo.paths.backups, now=clock.now)
    second = create_backup(repo._conn, repo.paths.backups, now=clock.now)
    assert first != second
    assert second.name.endswith("-1.sqlite3")
    for _ in range(BACKUP_KEEP + 3):
        clock.advance(seconds=1)
        create_backup(repo._conn, repo.paths.backups, now=clock.now)
    kept = list_backups(repo.paths.backups)
    assert len(kept) == BACKUP_KEEP
    assert first not in kept
    assert kept == sorted(kept)


def test_open_takes_a_startup_backup(paths: DataPaths, clock: FakeClock):
    repo = Repository.open(paths, clock=clock)
    try:
        assert len(list_backups(paths.backups)) == 1
    finally:
        repo.close()
    clock.advance(seconds=1)
    again = Repository.open(paths, clock=clock)
    try:
        assert len(list_backups(paths.backups)) == 2
    finally:
        again.close()


def held(lock: InstanceLock) -> bool:
    return lock.held


def test_instance_lock_excludes_a_second_holder(paths: DataPaths):
    paths.root.mkdir(parents=True)
    first = InstanceLock(paths.lock)
    first.acquire()
    assert held(first)
    assert stat.S_IMODE(paths.lock.stat().st_mode) == 0o600
    second = InstanceLock(paths.lock)
    with pytest.raises(AlreadyRunningError):
        second.acquire()
    first.release()
    assert not held(first)
    with second:
        assert held(second)
        with pytest.raises(AlreadyRunningError):
            first.acquire()
    assert not held(second)
    first.acquire()  # idempotent double acquire
    first.acquire()
    first.release()
    first.release()
