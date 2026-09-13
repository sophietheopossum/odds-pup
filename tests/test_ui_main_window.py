from __future__ import annotations

import csv
from pathlib import Path

import pytest
from PySide6.QtCore import QDate, QSettings

from odds_pup.core import BetStatus, LegResult, TwoWayOutcome
from odds_pup.storage import Repository
from odds_pup.ui.dialogs import BetDialogResult, SettleRequest
from odds_pup.ui.ledger_model import COLUMN_INDEX
from odds_pup.ui.main_window import MainWindow
from odds_pup.ui.settings import UiSettings
from tests.storage_helpers import FakeClock, f1_bet, snr_bet


@pytest.fixture
def window(qtbot, repo: Repository, clock: FakeClock, tmp_path: Path) -> MainWindow:
    settings = UiSettings(QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))
    win = MainWindow(repo, settings, clock=clock)
    errors: list[str] = []
    win.errors = errors  # type: ignore[attr-defined]
    win.show_error = errors.append
    win.confirm = lambda _title, _text: True
    qtbot.addWidget(win)
    win.show()
    return win


def shown_ids(window: MainWindow) -> set[str]:
    return {b.id for b in window.model.bets}


def test_window_lists_bets_and_updates_summary(window: MainWindow, repo: Repository):
    assert window.model.rowCount() == 0
    assert window.summary.counts.value.text() == "0 open · 0 settled"
    created = window.create_bet(f1_bet())
    assert created is not None
    assert shown_ids(window) == {created.id}
    assert window.selected_bet_id() == created.id
    assert window.summary.open_guaranteed.value.text() == "-£0.58"
    assert window.summary.open_liability.value.text() == "£10.58"
    assert "Arsenal v Chelsea" in window.detail.header.text()
    assert window.detail.legs.rowCount() == 2
    assert window.detail.audit.rowCount() == 1
    assert window.act_settle.isEnabled()
    assert not window.act_adjust.isEnabled()
    assert not window.act_reopen.isEnabled()


def test_settle_adjust_reopen_flow(window: MainWindow, repo: Repository, clock: FakeClock):
    bet = window.create_bet(f1_bet())
    assert bet is not None
    window.settle_selected(TwoWayOutcome.BACK_WON)
    settled = repo.get_bet(bet.id)
    assert settled.status is BetStatus.SETTLED
    assert window.summary.all_time.value.text() == "-£0.58"
    assert window.act_adjust.isEnabled()
    assert not window.act_settle.isEnabled()
    repo.adjust(bet.id, 0, "2-up paid out")
    window.refresh()
    assert window.summary.all_time.value.text() == "£0.00"
    row = window.model.row_of(bet.id)
    assert row is not None
    assert window.model.data(window.model.index(row, COLUMN_INDEX["Flags"])) == "adjusted"
    assert "Adjusted to" in window.detail.header.text()
    window.apply_settle(
        bet, SettleRequest(None, {}, clock.now)
    )  # nothing to settle -> warning path
    repo.reopen(bet.id)
    window.refresh()
    assert window.summary.all_time.value.text() == "£0.00"
    assert repo.get_bet(bet.id).status is BetStatus.OPEN


def test_per_leg_settle_through_window(window: MainWindow, repo: Repository, clock: FakeClock):
    bet = window.create_bet(f1_bet())
    assert bet is not None
    updated = window.apply_settle(
        bet, SettleRequest(None, {0: (LegResult.CASHED_OUT, 1000)}, clock.now)
    )
    assert updated is not None
    assert updated.status is BetStatus.PARTIALLY_SETTLED
    assert window.summary.open_guaranteed.value.text() == "-£0.58"
    assert window.act_settle.isEnabled()
    assert window.act_reopen.isEnabled()


def test_filters(qtbot, window: MainWindow, repo: Repository, clock: FakeClock):
    early = repo.create_bet(f1_bet(event_name="Early"))
    clock.advance(days=3)
    late = repo.create_bet(snr_bet(event_name="Late"))
    repo.settle_two_way(late.id, TwoWayOutcome.LAY_WON)
    window.refresh()
    assert shown_ids(window) == {early.id, late.id}
    window.status_filter.setCurrentIndex(2)  # Open
    assert shown_ids(window) == {early.id}
    window.status_filter.setCurrentIndex(0)
    window.search.setText("late")
    assert shown_ids(window) == {early.id, late.id}  # debounced: not yet re-queried
    qtbot.waitUntil(lambda: shown_ids(window) == {late.id}, timeout=2000)
    window.search.setText("")
    window.search.returnPressed.emit()  # Enter applies the search immediately
    assert shown_ids(window) == {early.id, late.id}
    index = window.bookmaker_filter.findData("William Hill")
    window.bookmaker_filter.setCurrentIndex(index)
    assert shown_ids(window) == {late.id}
    window.bookmaker_filter.setCurrentIndex(0)
    window.type_filter.setCurrentIndex(2)  # FREE_BET_SNR
    assert shown_ids(window) == {late.id}
    window.type_filter.setCurrentIndex(0)
    window.from_enabled.setChecked(True)
    window.from_date.setDate(QDate(2026, 9, 14))
    assert shown_ids(window) == {late.id}
    window.to_enabled.setChecked(True)
    window.to_date.setDate(QDate(2026, 9, 14))
    assert shown_ids(window) == set()
    window.from_enabled.setChecked(False)
    window.to_date.setDate(QDate(2026, 9, 12))
    assert shown_ids(window) == {early.id}
    window.to_enabled.setChecked(False)
    repo.soft_delete(early.id)
    window.refresh()
    assert shown_ids(window) == {late.id}
    window.show_deleted.setChecked(True)
    assert shown_ids(window) == {early.id, late.id}
    window.select_bet(early.id)
    assert window.act_delete.text() == "&Restore"
    window.delete_or_restore()
    assert not repo.get_bet(early.id).is_deleted
    assert window.status_label.text().startswith("2 bet(s)")


def test_edit_apply_and_review_toggle(window: MainWindow, repo: Repository):
    bet = window.create_bet(f1_bet())
    assert bet is not None
    edited_input = f1_bet(event_name="Renamed", notes="n", needs_review=True)
    updated = window.apply_edit(bet, BetDialogResult(edited_input))
    assert updated is not None
    assert updated.event_name == "Renamed"
    assert updated.needs_review
    window.toggle_review()
    assert not repo.get_bet(bet.id).needs_review
    repo.settle_two_way(bet.id, TwoWayOutcome.VOID)
    window.refresh()
    corrected = window.apply_edit(
        repo.get_bet(bet.id), BetDialogResult(f1_bet(event_name="Corrected"), reason="typo")
    )
    assert corrected is not None
    assert corrected.event_name == "Corrected"
    assert corrected.has_adjustments


def test_export_and_backup(window: MainWindow, repo: Repository, tmp_path: Path):
    window.create_bet(f1_bet())
    target = tmp_path / "ledger.csv"
    assert window.export_to(target) == 2
    with target.open(encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 3
    assert "Exported 2" in window.status_label.text()
    window.backup_now()
    assert "Backup written" in window.status_label.text()


def test_close_persists_geometry(window: MainWindow, tmp_path: Path):
    window.close()
    assert not window.settings.geometry().isEmpty()


def test_filter_changes_do_not_recompute_the_summary(
    window: MainWindow, repo: Repository, monkeypatch: pytest.MonkeyPatch
):
    repo.create_bet(f1_bet())
    window.refresh()
    calls = {"n": 0}
    real = repo.summary

    def counting(now=None):
        calls["n"] += 1
        return real(now)

    monkeypatch.setattr(repo, "summary", counting)
    window.status_filter.setCurrentIndex(2)
    window.review_only.setChecked(True)
    window.review_only.setChecked(False)
    assert calls["n"] == 0
    window.refresh()
    assert calls["n"] == 1


def test_venues_dialog_renames_and_sets_commission(qtbot, window: MainWindow, repo: Repository):
    from odds_pup.core import VenueKind
    from odds_pup.ui.venues_dialog import VenuesDialog

    bet = window.create_bet(f1_bet())
    assert bet is not None
    errors: list[str] = []
    dialog = VenuesDialog(repo, show_error=errors.append)
    qtbot.addWidget(dialog)
    smarkets = repo.find_venue("Smarkets")
    assert smarkets is not None
    assert dialog.apply_edit(
        smarkets,
        {
            "name": "Smarkets (2%)",
            "kind": VenueKind.EXCHANGE,
            "default_commission_bp": 150,
            "min_stake_pence": None,
            "ladder": None,
        },
    )
    assert repo.get_bet(bet.id).exchange == "Smarkets (2%)"
    assert dialog.changed
    added = dialog.apply_add(
        {
            "name": "Tiny Exchange",
            "kind": VenueKind.EXCHANGE,
            "default_commission_bp": 300,
            "min_stake_pence": 100,
            "ladder": None,
        }
    )
    assert added is not None
    row = next(i for i, v in enumerate(dialog._venues) if v.id == added.id)
    dialog.table.selectRow(row)
    assert dialog.delete_button.isEnabled()
    dialog.delete_selected()
    assert repo.find_venue("Tiny Exchange") is None
    bet365_row = next(i for i, v in enumerate(dialog._venues) if v.name == "Bet365")
    dialog.table.selectRow(bet365_row)
    assert not dialog.delete_button.isEnabled()  # used by a bet
    bet365 = repo.find_venue("Bet365")
    assert bet365 is not None
    assert (
        dialog.apply_edit(
            bet365,
            {
                "name": "Coral",
                "kind": VenueKind.BOOKMAKER,
                "default_commission_bp": 0,
                "min_stake_pence": None,
                "ladder": None,
            },
        )
        is None
    )
    assert errors
    assert "already exists" in errors[-1]


def test_correct_dialog_can_move_the_settled_time(qtbot, window: MainWindow, repo: Repository):
    from datetime import UTC, datetime

    from odds_pup.ui.dialogs import BetDialog, BetDialogMode
    from odds_pup.ui.format import to_qdatetime

    bet = window.create_bet(f1_bet())
    assert bet is not None
    settled = repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    dialog = BetDialog(venues=repo.list_venues(), mode=BetDialogMode.CORRECT, prefill=settled)
    qtbot.addWidget(dialog)
    when = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
    dialog.corrected_settled_at.setDateTime(to_qdatetime(when))
    dialog.reason.setText("backfilled with the wrong date")
    result = dialog.build_result()
    assert result.settled_at == when
    updated = window.apply_edit(settled, result)
    assert updated is not None
    assert updated.settled_at == when
    assert updated.expected_pl_pence == -58
