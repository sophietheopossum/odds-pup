from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette

from odds_pup.core import TwoWayOutcome
from odds_pup.storage import Repository
from odds_pup.ui.ledger_model import (
    BET_ID_ROLE,
    COLUMN_INDEX,
    COLUMNS,
    SORT_ROLE,
    LedgerModel,
    pl_colour,
)
from tests.storage_helpers import f1_bet, snr_bet


def test_model_exposes_spec_columns(qapp, repo: Repository):
    a = repo.create_bet(f1_bet())
    repo.settle_two_way(a.id, TwoWayOutcome.BACK_WON)
    b = repo.create_bet(snr_bet())
    model = LedgerModel(repo.list_bets())
    assert model.rowCount() == 2
    assert model.columnCount() == len(COLUMNS)
    titles = [model.headerData(i, Qt.Orientation.Horizontal) for i in range(len(COLUMNS))]
    assert titles[:6] == ["Placed", "Event", "Selection", "Type", "Bookmaker", "Exchange"]
    row_a = model.row_of(a.id)
    assert row_a is not None

    def text(name: str) -> str:
        return str(model.data(model.index(row_a, COLUMN_INDEX[name])))

    assert text("Event") == "Arsenal v Chelsea"
    assert text("Type") == "QL"
    assert text("Bookmaker") == "Bet365"
    assert text("Back stake") == "£10.00"
    assert text("Lay odds") == "2.10"
    assert text("Expected") == "-£0.58"
    assert text("Actual") == "-£0.58"
    assert text("Status") == "Settled"
    row_b = model.row_of(b.id)
    assert row_b is not None
    assert model.data(model.index(row_b, COLUMN_INDEX["Actual"])) == ""
    assert model.data(model.index(row_b, COLUMN_INDEX["Expected"])) == "+£4.71"
    assert model.data(model.index(row_b, 0), BET_ID_ROLE) == b.id
    assert model.data(model.index(row_b, COLUMN_INDEX["Expected"]), SORT_ROLE) == 471
    assert model.data(
        model.index(row_a, COLUMN_INDEX["Back stake"]), Qt.ItemDataRole.TextAlignmentRole
    )


def test_pl_colour_is_secondary_and_theme_aware(qapp):
    light = QPalette()
    light.setColor(QPalette.ColorRole.Window, QColor("#ffffff"))
    dark = QPalette()
    dark.setColor(QPalette.ColorRole.Window, QColor("#202020"))
    assert pl_colour(0, light) is None
    assert pl_colour(None, light) is None
    assert pl_colour(100, light) != pl_colour(100, dark)
    assert pl_colour(-100, light) != pl_colour(100, light)


def test_audit_values_render_by_kind():
    from odds_pup.ui.format import audit_field_text, audit_value_text

    assert audit_field_text("legs[1].commission_bp") == "Leg 2 commission"
    assert audit_field_text("actual_pl_override_pence") == "Adjusted P/L"
    assert audit_value_text("legs[1].commission_bp", "200") == "2%"
    assert audit_value_text("actual_pl_override_pence", "-20") == "-£0.20"
    assert audit_value_text("expected_pl_pence", "471") == "+£4.71"
    assert audit_value_text("legs[0].odds", "2.3750") == "2.375"
    assert audit_value_text("settled_at", "2026-08-31T23:30:00Z") == "01/09/2026 00:30"
    assert audit_value_text("needs_review", "1") == "Yes"
    assert audit_value_text("legs[0].result", "CASHED_OUT") == "Cashed out"
    assert audit_value_text("bet_type", "FREE_BET_SNR") == "Free bet (SNR)"
    assert audit_value_text(None, "backfill") == "backfilled"
    assert audit_value_text("legs[0].stake", "not-a-number") == "not-a-number"
