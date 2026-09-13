from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from odds_pup.core import VenueKind
from odds_pup.storage import (
    SCHEMA_VERSION,
    SEED_VENUES,
    DataPaths,
    NewerDatabaseError,
    Repository,
    connect,
    migrate,
    schema_version,
    transaction,
)
from odds_pup.storage import database as database_module
from odds_pup.storage.schema import MIGRATIONS
from tests.storage_helpers import FakeClock, f1_bet


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_open_creates_owner_only_files_and_seeds(paths: DataPaths, clock: FakeClock):
    repo = Repository.open(paths, clock=clock, backups=False)
    try:
        assert mode(paths.root) == 0o700
        assert mode(paths.backups) == 0o700
        assert mode(paths.database) == 0o600
        assert schema_version(repo._conn) == SCHEMA_VERSION
        assert repo._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert repo._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert repo._conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        tables = {
            r[0] for r in repo._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"venues", "offers", "bets", "bet_legs", "adjustments"} <= tables
        assert len(repo.list_venues()) == len(SEED_VENUES)
    finally:
        repo.close()


def test_reopen_does_not_reseed_or_duplicate(paths: DataPaths, clock: FakeClock):
    first = Repository.open(paths, clock=clock, backups=False)
    first.ensure_venue("Tiny Bookie", VenueKind.BOOKMAKER)
    count = len(first.list_venues())
    first.close()
    second = Repository.open(paths, clock=clock, backups=False)
    try:
        assert len(second.list_venues()) == count
    finally:
        second.close()


def test_newer_database_is_refused(paths: DataPaths, clock: FakeClock):
    paths.root.mkdir(parents=True)
    conn = sqlite3.connect(paths.database)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(NewerDatabaseError):
        Repository.open(paths, clock=clock, backups=False)


def test_migration_hook_runs_only_for_an_existing_database(
    paths: DataPaths, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
):
    calls: list[str] = []
    paths.root.mkdir(parents=True)
    conn = connect(paths.database)
    migrate(conn, before_migration=lambda: calls.append("fresh"))
    assert calls == []  # a brand-new file is not backed up first
    assert schema_version(conn) == 1

    extra = (2, "CREATE TABLE extra (x INTEGER) STRICT;\nPRAGMA user_version = 2;")
    monkeypatch.setattr(database_module, "MIGRATIONS", (*MIGRATIONS, extra))
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 2)
    assert migrate(conn, before_migration=lambda: calls.append("upgrade")) == 2
    assert calls == ["upgrade"]
    assert schema_version(conn) == 2
    assert migrate(conn, before_migration=lambda: calls.append("again")) == 2
    assert calls == ["upgrade"]
    conn.close()


def _insert_then_fail(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO venues (id, name, kind, created_at) VALUES"
            " ('x', 'Rollback Bookie', 'BOOKMAKER', '2026-09-12T00:00:00Z')"
        )
        raise RuntimeError("boom")


def test_transaction_rolls_back_on_error(repo: Repository):
    conn = repo._conn
    with pytest.raises(RuntimeError):
        _insert_then_fail(conn)
    assert repo.find_venue("Rollback Bookie") is None
    assert conn.in_transaction is False


def test_audit_table_is_append_only(repo: Repository):
    bet = repo.create_bet(f1_bet())
    row = repo._conn.execute("SELECT id FROM adjustments WHERE bet_id = ?", (bet.id,)).fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute("UPDATE adjustments SET reason = 'x' WHERE id = ?", (row["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute("DELETE FROM adjustments WHERE id = ?", (row["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute(
            "INSERT INTO adjustments (id, bet_id, action_id, at, kind, reason) VALUES"
            " ('a', ?, 'act', '2026-09-12T00:00:00Z', 'ADJUSTMENT', NULL)",
            (bet.id,),
        )


def test_spec_ddl_matches_the_shipped_schema():
    import re

    from odds_pup.storage.schema import DDL_V1

    spec = (Path(__file__).resolve().parents[1] / "docs" / "SPEC.md").read_text(encoding="utf-8")
    match = re.search(r"```sql\n(.*?)```", spec, re.S)
    assert match is not None
    assert match[1].strip() == DDL_V1.strip()
