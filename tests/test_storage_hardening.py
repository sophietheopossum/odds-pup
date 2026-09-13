"""Durability, locking, permissions, error translation and backups (storage review findings)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from odds_pup.core import Side, TwoWayOutcome
from odds_pup.storage import (
    AlreadyRunningError,
    CorruptDatabaseError,
    DatabaseBusyError,
    DataPaths,
    DiskFullError,
    InstanceLock,
    MigrationError,
    NewerDatabaseError,
    NewLeg,
    Repository,
    create_backup,
    list_backups,
    resolve_data_dir,
    schema_version,
)
from odds_pup.storage import database as database_module
from odds_pup.storage.schema import MIGRATIONS
from tests.storage_helpers import FakeClock, f1_bet


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def lock_is_free(paths: DataPaths) -> bool:
    probe = InstanceLock(paths.lock)
    try:
        probe.acquire()
    except AlreadyRunningError:
        return False
    probe.release()
    return True


# ---------------------------------------------------------------- open / lock


def test_open_holds_the_instance_lock_until_close(paths: DataPaths, clock: FakeClock):
    first = Repository.open(paths, clock=clock, backups=False)
    with pytest.raises(AlreadyRunningError):
        Repository.open(paths, clock=clock, backups=False)
    first.close()
    assert lock_is_free(paths)
    second = Repository.open(paths, clock=clock, backups=False)
    second.close()


def test_newer_database_is_refused_untouched_and_everything_released(
    paths: DataPaths, clock: FakeClock
):
    paths.root.mkdir(parents=True)
    conn = sqlite3.connect(paths.database)
    conn.execute("CREATE TABLE future (x INTEGER)")
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    before = hashlib.sha256(paths.database.read_bytes()).hexdigest()
    with pytest.raises(NewerDatabaseError):
        Repository.open(paths, clock=clock)
    assert hashlib.sha256(paths.database.read_bytes()).hexdigest() == before
    assert not Path(f"{paths.database}-wal").exists()
    assert not Path(f"{paths.database}-shm").exists()
    assert list_backups(paths.backups) == []
    assert lock_is_free(paths)


def test_corrupt_file_is_a_corrupt_database_error(paths: DataPaths, clock: FakeClock):
    paths.root.mkdir(parents=True)
    paths.database.write_bytes(os.urandom(4096))
    with pytest.raises(CorruptDatabaseError):
        Repository.open(paths, clock=clock)
    assert lock_is_free(paths)


def test_failed_migration_rolls_back_and_releases(
    paths: DataPaths, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
):
    Repository.open(paths, clock=clock, backups=False).close()
    broken = (
        2,
        "CREATE TABLE extra (x INTEGER) STRICT;\n"
        "INSERT INTO nope VALUES (1);\n"
        "PRAGMA user_version = 2;",
    )
    monkeypatch.setattr(database_module, "MIGRATIONS", (*MIGRATIONS, broken))
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 2)
    clock.advance(seconds=1)
    with pytest.raises(MigrationError):
        Repository.open(paths, clock=clock)
    assert len(list_backups(paths.backups)) == 1  # the pre-migration backup was taken
    assert lock_is_free(paths)
    conn = sqlite3.connect(paths.database)
    try:
        assert schema_version(conn) == 1
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name = 'extra'").fetchone() is None
        )
    finally:
        conn.close()


def test_migrate_leaves_the_connection_usable_after_failure(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
):
    paths.root.mkdir(parents=True)
    conn = database_module.connect(paths.database)
    database_module.migrate(conn)
    broken = (2, "CREATE TABLE extra (x INTEGER) STRICT;\nCREATE TABLE extra (x INTEGER) STRICT;")
    monkeypatch.setattr(database_module, "MIGRATIONS", (*MIGRATIONS, broken))
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 2)
    with pytest.raises(MigrationError):
        database_module.migrate(conn)
    assert conn.in_transaction is False
    with database_module.transaction(conn):
        conn.execute("SELECT 1")
    conn.close()


# ------------------------------------------------------------ error translation


def test_disk_full_surfaces_as_disk_full(repo: Repository):
    conn = repo._conn
    conn.execute(f"PRAGMA max_page_count = {conn.execute('PRAGMA page_count').fetchone()[0] + 1}")

    def fill() -> None:
        for _ in range(200):
            repo.create_bet(f1_bet(notes="x" * 4000))

    with pytest.raises(DiskFullError):
        fill()
    assert conn.in_transaction is False
    conn.execute("PRAGMA max_page_count = 1073741823")
    repo.create_bet(f1_bet())  # the connection still works


def test_foreign_write_lock_is_a_busy_error(repo: Repository, paths: DataPaths):
    bet = repo.create_bet(f1_bet())
    repo._conn.execute("PRAGMA busy_timeout = 50")
    other = sqlite3.connect(paths.database, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(DatabaseBusyError):
            repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert repo._conn.in_transaction is False
    assert repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON).actual_pl_pence == -58


def test_a_failing_write_mid_action_leaves_no_trace(
    repo: Repository, monkeypatch: pytest.MonkeyPatch
):
    bet = repo.create_bet(f1_bet())
    before = repo.get_bet(bet.id)
    trail = len(repo.audit_trail(bet.id))
    real_audit = repo._audit
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # after leg 0's UPDATE and its first audit row
            raise RuntimeError("power cut")
        return real_audit(*args, **kwargs)

    monkeypatch.setattr(repo, "_audit", flaky)
    with pytest.raises(RuntimeError):
        repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    monkeypatch.undo()
    assert repo.get_bet(bet.id) == before
    assert len(repo.audit_trail(bet.id)) == trail
    assert repo._conn.in_transaction is False


# ------------------------------------------------------------------ permissions


def test_existing_loose_files_are_tightened(paths: DataPaths, clock: FakeClock):
    old_umask = os.umask(0o022)
    try:
        paths.root.mkdir(mode=0o755)
        sqlite3.connect(paths.database).close()
        os.chmod(paths.database, 0o644)
        paths.lock.touch(mode=0o644)
        os.chmod(paths.lock, 0o644)
        repo = Repository.open(paths, clock=clock)
        repo.create_bet(f1_bet())
        try:
            assert mode(paths.root) == 0o700
            assert mode(paths.database) == 0o600
            assert mode(Path(f"{paths.database}-wal")) == 0o600
            assert mode(Path(f"{paths.database}-shm")) == 0o600
            assert mode(paths.lock) == 0o600
        finally:
            repo.close()
    finally:
        os.umask(old_umask)


def test_instance_lock_creates_its_directory(tmp_path: Path):
    lock = InstanceLock(tmp_path / "fresh" / "odds-pup.lock")
    lock.acquire()
    assert mode(tmp_path / "fresh") == 0o700
    lock.release()


# --------------------------------------------------------------------- backups


def test_same_second_backups_rotate_newest_last(repo: Repository, clock: FakeClock):
    assert repo.paths is not None
    target = repo.paths.backups
    made = [create_backup(repo._conn, target, now=clock.now, keep=3) for _ in range(3)]
    assert [p.name for p in list_backups(target)] == [p.name for p in made]
    clock.advance(seconds=1)
    newest = create_backup(repo._conn, target, now=clock.now, keep=3)
    kept = list_backups(target)
    assert made[0] not in kept  # the oldest (base name) went first
    assert kept == [made[1], made[2], newest]


def test_backup_keep_must_be_positive_and_missing_dir_lists_nothing(
    repo: Repository, tmp_path: Path
):
    with pytest.raises(ValueError, match="keep"):
        create_backup(repo._conn, tmp_path / "b", now=datetime(2026, 9, 12, tzinfo=UTC), keep=0)
    assert list_backups(tmp_path / "missing") == []


def test_resolve_data_dir_precedence(monkeypatch: pytest.MonkeyPatch):
    import platformdirs

    monkeypatch.setattr(platformdirs, "user_data_dir", lambda _name: "/fallback/odds-pup")
    env = {"ODDS_PUP_DATA_DIR": "/from/env"}
    assert resolve_data_dir("/explicit", env=env) == Path("/explicit")
    assert resolve_data_dir(None, env=env) == Path("/from/env")
    assert resolve_data_dir(None, env={"ODDS_PUP_DATA_DIR": ""}) == Path("/fallback/odds-pup")
    assert resolve_data_dir("~/x", env={}) == Path.home() / "x"


# ------------------------------------------------------------------ performance


def test_venue_filters_are_not_correlated_subqueries(repo: Repository):
    repo.create_bet(f1_bet())
    captured: list[str] = []
    repo._conn.set_trace_callback(captured.append)
    try:
        from odds_pup.storage import BetFilter

        repo.list_bets(BetFilter(bookmaker="Bet365", exchange="Smarkets"))
    finally:
        repo._conn.set_trace_callback(None)
    query = next(sql for sql in captured if "FROM bets b" in sql)
    plan = repo._conn.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    assert not any("CORRELATED" in str(row["detail"]) for row in plan)


def test_lay_at_unknown_name_creates_an_exchange(repo: Repository):
    bet = repo.create_bet(
        f1_bet(
            legs=[
                NewLeg(Side.BACK, "Bet365", f1_bet().legs[0].odds, 1000),
                NewLeg(
                    Side.LAY, "Brand New Exchange", f1_bet().legs[1].odds, 962, commission_bp=150
                ),
            ]
        )
    )
    assert bet.legs[1].leg.commission_bp == 150
