"""Regression tests for defects found by the adversarial UI review (each reproduced first)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QDialog

from odds_pup.core import LegResult, Side, TwoWayOutcome, VenueKind
from odds_pup.storage import BetRecord, IllegalActionError, NewBet, NewLeg, Repository
from odds_pup.ui.dialogs import BetDialog, BetDialogMode, BetDialogResult, SettleDialog
from odds_pup.ui.ledger_model import COLUMN_INDEX
from odds_pup.ui.main_window import MainWindow
from odds_pup.ui.settings import UiSettings
from odds_pup.ui.summary_strip import StatTile
from odds_pup.ui.venues_dialog import VenuesDialog
from tests.storage_helpers import FakeClock, f1_bet, snr_bet

D = Decimal


@pytest.fixture
def ui_settings(tmp_path: Path) -> UiSettings:
    return UiSettings(QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))


@pytest.fixture
def window(qtbot, repo: Repository, clock: FakeClock, ui_settings: UiSettings) -> MainWindow:
    win = MainWindow(repo, ui_settings, clock=clock)
    errors: list[str] = []
    win.errors = errors  # type: ignore[attr-defined]
    win.show_error = errors.append
    win.confirm = lambda _title, _text: True
    qtbot.addWidget(win)
    win.show()
    return win


def fill_f1(dialog: BetDialog) -> None:
    dialog.event_name.setText("Arsenal v Chelsea")
    dialog.bookmaker.setCurrentText("Bet365")
    dialog.back_stake.setText("10")
    dialog.back_odds.setText("2.00")
    dialog.exchange.setCurrentText("Smarkets")
    dialog.commission.setText("2")
    dialog.lay_odds.setText("2.10")


def lay_950_bet() -> NewBet:
    return f1_bet(
        legs=[f1_bet().legs[0], NewLeg(Side.LAY, "Smarkets", D("2.10"), 950, commission_bp=200)]
    )


# ------------------------------------------------------------------ Enter key


def test_enter_in_a_field_saves_and_keeps_a_hand_typed_lay_stake(qtbot, repo: Repository):
    saved: list[BetDialogResult] = []
    dialog = BetDialog(venues=repo.list_venues(), save=saved.append)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    fill_f1(dialog)
    dialog.lay_stake.setText("9.50")
    dialog.lay_stake.textEdited.emit("9.50")
    assert dialog.save_button.isDefault()
    dialog.event_name.setFocus()
    qtbot.keyClick(dialog.event_name, Qt.Key.Key_Return)
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert saved[0].bet.legs[1].stake == 950


def test_enter_in_edit_does_not_swap_in_the_suggested_stake(qtbot, window: MainWindow, repo):
    bet = window.create_bet(lay_950_bet())
    assert bet is not None
    saved: list[BetRecord] = []
    dialog = window._bet_dialog(
        BetDialogMode.EDIT, bet, lambda r: saved.append(window.write_edit(bet, r))
    )
    assert dialog is not None
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    qtbot.keyClick(dialog.event_name, Qt.Key.Key_Return)
    assert dialog.lay_stake.text() == "9.50"
    assert repo.get_bet(bet.id).legs[1].leg.stake == 950
    assert len(repo.audit_trail(bet.id)) == 1  # an unchanged save writes nothing


def test_enter_in_the_settle_editor_applies_per_leg_results(qtbot, repo: Repository):
    bet = repo.create_bet(f1_bet())
    dialog = SettleDialog(bet)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    assert dialog.apply_button.isDefault()
    assert not any(b.isDefault() or b.autoDefault() for b in dialog.outcome_buttons.values())
    back_row = dialog.rows[0]
    back_row.result.setCurrentIndex(back_row.result.findData(LegResult.CASHED_OUT))
    back_row.amount.setText("20.00")
    qtbot.keyClick(back_row.amount, Qt.Key.Key_Return)
    assert dialog.result_value is not None
    assert dialog.result_value.outcome is None
    assert dialog.result_value.per_leg == {0: (LegResult.CASHED_OUT, 1000)}


# ------------------------------------------------------------------ silent defaults


def test_blank_manual_lay_stake_and_blank_commission_block_saving(qtbot, repo: Repository):
    dialog = BetDialog(venues=repo.list_venues())
    qtbot.addWidget(dialog)
    fill_f1(dialog)
    dialog.lay_stake.setText("")
    dialog.lay_stake.textEdited.emit("")
    assert not dialog.save_button.isEnabled()
    assert "lay stake" in dialog.error_label.text()
    assert dialog.use_suggested.isEnabled()
    dialog.use_suggested.click()
    assert dialog.save_button.isEnabled()
    dialog.commission.setText("")
    assert not dialog.save_button.isEnabled()
    assert "commission" in dialog.error_label.text()


def test_exchange_field_naming_a_bookmaker_blocks_saving(qtbot, repo: Repository):
    dialog = BetDialog(venues=repo.list_venues())
    qtbot.addWidget(dialog)
    fill_f1(dialog)
    dialog.exchange.setCurrentText("Coral")
    assert not dialog.save_button.isEnabled()
    assert "Coral is a bookmaker" in dialog.error_label.text()


def test_refused_save_keeps_the_dialog_open_with_input(qtbot, repo: Repository):
    def refuse(_result: BetDialogResult) -> None:
        raise IllegalActionError("storage said no")

    dialog = BetDialog(venues=repo.list_venues(), save=refuse)
    qtbot.addWidget(dialog)
    dialog.show()
    fill_f1(dialog)
    dialog.save_button.click()
    assert dialog.isVisible()
    assert dialog.result_value is None
    assert dialog.error_label.text() == "storage said no"
    assert dialog.event_name.text() == "Arsenal v Chelsea"


def test_new_exchange_remembers_the_commission_it_was_first_used_with(repo: Repository):
    repo.create_bet(
        f1_bet(
            legs=[
                f1_bet().legs[0],
                NewLeg(Side.LAY, "Betconnect", D("2.1"), 962, commission_bp=250),
            ]
        )
    )
    venue = repo.find_venue("Betconnect")
    assert venue is not None
    assert (venue.kind, venue.default_commission_bp) == (VenueKind.EXCHANGE, 250)


# ------------------------------------------------------------------ parent link and settings


def test_editing_keeps_a_parent_older_than_the_candidate_list(
    qtbot, window: MainWindow, repo, clock
):
    old = repo.create_bet(f1_bet(event_name="Old QL"))
    for _ in range(55):
        clock.advance(minutes=1)
        repo.create_bet(f1_bet())
    child = repo.create_bet(snr_bet(parent_bet_id=old.id))
    dialog = window._bet_dialog(BetDialogMode.EDIT, child)
    assert dialog is not None
    qtbot.addWidget(dialog)
    assert dialog.parent_bet.currentData() == old.id
    window.write_edit(child, dialog.build_result())
    assert repo.get_bet(child.id).parent_bet_id == old.id
    repo.soft_delete(old.id)
    again = window._bet_dialog(BetDialogMode.EDIT, repo.get_bet(child.id))
    assert again is not None
    qtbot.addWidget(again)
    window.write_edit(child, again.build_result())
    assert repo.get_bet(child.id).parent_bet_id == old.id
    assert len(repo.audit_trail(child.id)) == 1


def test_clone_lists_its_source_as_a_possible_parent(qtbot, window: MainWindow, repo):
    source = repo.create_bet(f1_bet())
    clone = window._bet_dialog(BetDialogMode.NEW, source)
    edit = window._bet_dialog(BetDialogMode.EDIT, source)
    assert clone is not None and edit is not None  # noqa: PT018
    qtbot.addWidget(clone)
    qtbot.addWidget(edit)
    assert clone.parent_bet.findData(source.id) >= 0
    assert edit.parent_bet.findData(source.id) < 0


def test_settings_follow_new_bets_only_and_skip_renamed_venues(qtbot, repo, ui_settings):
    ui_settings.last_bookmaker = "Bet365"
    ui_settings.last_exchange = "Smarkets"
    bet = repo.create_bet(
        f1_bet(
            legs=[
                NewLeg(Side.BACK, "Coral", D("2.0"), 1000),
                NewLeg(Side.LAY, "Matchbook", D("2.1"), 962, commission_bp=0),
            ]
        )
    )
    edit = BetDialog(
        venues=repo.list_venues(), settings=ui_settings, mode=BetDialogMode.EDIT, prefill=bet
    )
    qtbot.addWidget(edit)
    edit.save_button.click()
    assert ui_settings.last_exchange == "Smarkets"
    smarkets = repo.find_venue("Smarkets")
    assert smarkets is not None
    repo.update_venue(smarkets.id, name="Smarkets Exchange")
    fresh = BetDialog(venues=repo.list_venues(), settings=ui_settings)
    qtbot.addWidget(fresh)
    assert fresh.exchange.currentText() == ""
    assert fresh.bookmaker.currentText() == "Bet365"


def test_correction_reason_is_recorded_for_a_review_flag_change(qtbot, window: MainWindow, repo):
    bet = repo.create_bet(f1_bet())
    settled = repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    dialog = BetDialog(venues=repo.list_venues(), mode=BetDialogMode.CORRECT, prefill=settled)
    qtbot.addWidget(dialog)
    dialog.needs_review.setChecked(True)
    dialog.reason.setText("stake looks wrong on the statement")
    window.write_edit(settled, dialog.build_result())
    row = repo.audit_trail(bet.id)[-1]
    assert (row.field, row.reason) == ("needs_review", "stake looks wrong on the statement")


# ------------------------------------------------------------------ sorting, keyboard, a11y


def test_placed_odds_and_actual_columns_sort(
    window: MainWindow, repo: Repository, clock: FakeClock
):
    odds = ["3.0", "1.5", "2.25"]
    for index, price in enumerate(odds):
        clock.advance(hours=1)
        legs = [
            NewLeg(Side.BACK, "Bet365", D(price), 1000),
            NewLeg(Side.LAY, "Smarkets", D(price), 1000, commission_bp=200),
        ]
        bet = repo.create_bet(f1_bet(event_name=f"E{index}", legs=legs))
        if index != 1:
            repo.settle_two_way(
                bet.id, TwoWayOutcome.BACK_WON if index == 0 else TwoWayOutcome.LAY_WON
            )
    window.refresh()

    def column(name: str) -> list[str]:
        col = COLUMN_INDEX[name]
        return [window.proxy.index(r, col).data() for r in range(window.proxy.rowCount())]

    window.table.sortByColumn(COLUMN_INDEX["Placed"], Qt.SortOrder.AscendingOrder)
    assert column("Event") == ["E0", "E1", "E2"]
    window.table.sortByColumn(COLUMN_INDEX["Placed"], Qt.SortOrder.DescendingOrder)
    assert column("Event") == ["E2", "E1", "E0"]
    window.table.sortByColumn(COLUMN_INDEX["Back odds"], Qt.SortOrder.AscendingOrder)
    assert column("Back odds") == ["1.50", "2.25", "3.00"]
    window.table.sortByColumn(COLUMN_INDEX["Actual"], Qt.SortOrder.AscendingOrder)
    actual = column("Actual")
    assert actual[0] == ""  # unsettled first
    values = [Decimal(a.replace("£", "").replace("+", "")) for a in actual[1:]]
    assert values == sorted(values)


def test_tab_leaves_tables_and_ctrl_f_finds(qtbot, window: MainWindow):
    for table in (window.table, window.detail.legs, window.detail.audit, window.bookmaker_table):
        assert not table.tabKeyNavigation()
    assert window.act_find.shortcut().toString() == "Ctrl+F"
    assert window.act_find in window.actions()  # window-wide, even without a menu entry
    window.table.setFocus()
    window.search.setText("arsenal")
    window.act_find.trigger()
    assert window.focusWidget() is window.search  # offscreen windows are never active
    assert window.search.selectedText() == "arsenal"


def test_value_labels_expose_their_values_to_screen_readers(qtbot, repo: Repository):
    dialog = BetDialog(venues=repo.list_venues())
    qtbot.addWidget(dialog)
    fill_f1(dialog)
    assert dialog.guaranteed_label.accessibleName() == "Guaranteed: -£0.58"
    assert dialog.warnings_label.accessibleName() == ""
    tile = StatTile("Open liability")
    qtbot.addWidget(tile)
    tile.show_text("£10.58")
    assert tile.value.accessibleName() == "Open liability: £10.58"


def test_tile_fonts_follow_the_application_font(qtbot):
    original = QApplication.font()
    big = QFont(original)
    big.setPointSizeF(20)
    QApplication.setFont(big)
    try:
        tile = StatTile("Realised")
        qtbot.addWidget(tile)
        assert tile.value.font().pointSizeF() == pytest.approx(30)
        assert tile.caption.font().pointSizeF() == pytest.approx(17)
    finally:
        QApplication.setFont(original)


def test_notes_tab_moves_focus(qtbot, repo: Repository):
    dialog = BetDialog(venues=repo.list_venues())
    qtbot.addWidget(dialog)
    assert dialog.notes.tabChangesFocus()


def test_enter_on_a_venue_edits_it(qtbot, repo: Repository, monkeypatch: pytest.MonkeyPatch):
    dialog = VenuesDialog(repo)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    edited: list[str] = []
    added: list[str] = []
    monkeypatch.setattr(dialog, "edit_selected", lambda: edited.append("edit"))
    monkeypatch.setattr(dialog, "add_venue", lambda: added.append("add"))
    dialog.table.activated.disconnect()
    dialog.table.activated.connect(lambda _index: dialog.edit_selected())
    dialog.table.selectRow(0)
    dialog.table.setFocus()
    qtbot.keyClick(dialog.table, Qt.Key.Key_Return)
    assert edited == ["edit"]
    assert added == []
    assert not dialog.add_button.autoDefault()


def test_new_bet_uses_the_exchange_default_not_the_last_rate(qtbot, repo: Repository, ui_settings):
    ui_settings.last_exchange = "Betfair Exchange"
    first = BetDialog(venues=repo.list_venues(), settings=ui_settings)
    qtbot.addWidget(first)
    assert first.commission.text() == "5"
    betfair = repo.find_venue("Betfair Exchange")
    assert betfair is not None
    repo.update_venue(betfair.id, default_commission_bp=200)  # the user sets their real rate
    second = BetDialog(venues=repo.list_venues(), settings=ui_settings)
    qtbot.addWidget(second)
    assert second.exchange.currentText() == "Betfair Exchange"
    assert second.commission.text() == "2"


def test_tests_never_use_the_desktop_display():
    import os

    assert os.environ["QT_QPA_PLATFORM"] == "offscreen"
    assert QApplication.platformName() == "offscreen"
