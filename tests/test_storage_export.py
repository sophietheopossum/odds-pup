from __future__ import annotations

import csv
import io
import stat
from pathlib import Path

from odds_pup.core import BetStatus, TwoWayOutcome
from odds_pup.storage import CSV_COLUMNS, BetFilter, Repository, export_csv, write_csv
from tests.storage_helpers import f1_bet, snr_bet


def test_write_csv_one_row_per_leg(repo: Repository):
    bet = repo.create_bet(f1_bet(notes='has "quotes", commas'))
    repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    repo.create_bet(snr_bet())
    out = io.StringIO()
    assert write_csv(repo, out) == 4
    rows = list(csv.reader(io.StringIO(out.getvalue())))
    assert rows[0] == list(CSV_COLUMNS)
    assert len(rows) == 5
    by_col = [dict(zip(CSV_COLUMNS, r, strict=True)) for r in rows[1:]]
    back = next(r for r in by_col if r["bet_id"] == bet.id and r["side"] == "BACK")
    lay = next(r for r in by_col if r["bet_id"] == bet.id and r["side"] == "LAY")
    assert back["odds_pup_schema_version"] == "1"
    assert back["status"] == "SETTLED"
    assert back["expected_pl"] == "-0.58"
    assert back["actual_pl"] == "-0.58"
    assert back["effective_pl"] == "-0.58"
    assert back["override_pl"] == ""
    assert back["stake"] == "10.00"
    assert back["odds"] == "2.0000"
    assert back["result"] == "WON"
    assert back["notes"] == 'has "quotes", commas'
    assert lay["commission"] == "2%"
    assert lay["venue"] == "Smarkets"
    assert lay["settled_at"].endswith("Z")


def test_export_respects_filter_and_permissions(repo: Repository, tmp_path: Path):
    repo.create_bet(f1_bet())
    settled = repo.create_bet(f1_bet(event_name="Done"))
    repo.settle_two_way(settled.id, TwoWayOutcome.VOID)
    target = tmp_path / "out.csv"
    count = export_csv(repo, target, BetFilter(statuses=frozenset({BetStatus.VOID})))
    assert count == 2
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    text = target.read_text(encoding="utf-8")
    assert text.count("\n") == 3
    assert "Done" in text
