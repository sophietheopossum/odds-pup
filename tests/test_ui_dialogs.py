from __future__ import annotations

from decimal import Decimal

import pytest
from PySide6.QtCore import QSettings

from odds_pup.core import (
    BetType,
    LegResult,
    Side,
    StakeKind,
    TwoWayOutcome,
    ValidationError,
    VenueKind,
)
from odds_pup.storage import NewBet, NewLeg, Repository
from odds_pup.ui.dialogs import (
    AdjustDialog,
    BetDialog,
    BetDialogMode,
    SettleDialog,
    stake_kind_for,
)
from odds_pup.ui.settings import UiSettings
from tests.storage_helpers import FakeClock, f1_bet


@pytest.fixture
def ui_settings(tmp_path):
    return UiSettings(QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat))


def fill_f1(dialog: BetDialog) -> None:
    dialog.event_name.setText("Arsenal v Chelsea")
    dialog.selection.setText("Arsenal")
    dialog.bookmaker.setCurrentText("Bet365")
    dialog.back_stake.setText("10")
    dialog.back_odds.setText("2.00")
    dialog.exchange.setCurrentText("Smarkets")
    dialog.commission.setText("2")
    dialog.lay_odds.setText("2.10")


def test_bet_dialog_live_panel_and_result(qtbot, repo: Repository, ui_settings, clock: FakeClock):
    dialog = BetDialog(venues=repo.list_venues(), settings=ui_settings, clock=clock)
    qtbot.addWidget(dialog)
    assert not dialog.save_button.isEnabled()
    fill_f1(dialog)
    position = dialog.position()
    assert position is not None
    assert dialog.lay_stake.text() == "9.62"
    assert dialog.liability_label.text() == "£10.58"
    assert dialog.back_wins_label.text() == "-£0.58"
    assert dialog.lay_wins_label.text() == "-£0.57"
    assert dialog.guaranteed_label.text() == "-£0.58"
    assert dialog.rating_label.text() == "94.23%"
    assert dialog.warnings_label.text() == ""
    assert dialog.save_button.isEnabled()

    result = dialog.build_result()
    bet = result.bet
    assert bet.bet_type is BetType.QUALIFYING
    assert bet.event_name == "Arsenal v Chelsea"
    assert [leg.side for leg in bet.legs] == [Side.BACK, Side.LAY]
    assert bet.legs[0].venue == "Bet365"
    assert bet.legs[1].venue == "Smarkets"
    assert bet.legs[1].stake == 962
    assert bet.legs[1].commission_bp == 200
    assert bet.placed_at == clock.now
    assert bet.settled_at is None

    created = repo.create_bet(bet)
    assert created.expected_pl_pence == -58


def test_bet_dialog_exchange_prefills_commission_and_flags_ladder(qtbot, repo: Repository):
    dialog = BetDialog(venues=repo.list_venues())
    qtbot.addWidget(dialog)
    fill_f1(dialog)
    dialog.exchange.setCurrentText("Betfair Exchange")
    assert dialog.commission.text() == "5"
    dialog.lay_odds.setText("2.11")
    assert "not a Betfair price" in dialog.warnings_label.text()
    dialog.back_stake.setText("1")
    assert "below the exchange minimum" in dialog.warnings_label.text()


def test_bet_dialog_manual_lay_stake_and_fraction_text(qtbot, repo: Repository):
    dialog = BetDialog(venues=repo.list_venues())
    qtbot.addWidget(dialog)
    fill_f1(dialog)
    dialog.back_odds.setText("evens")
    dialog.lay_stake.setText("10.00")
    dialog.lay_stake.textEdited.emit("10.00")
    position = dialog.position()
    assert position is not None
    assert position.lay.stake == 1000
    assert dialog.back_wins_label.text() == "-£1.00"  # F7
    assert dialog.use_suggested.isEnabled()
    dialog.use_suggested.click()
    assert dialog.lay_stake.text() == "9.62"
    bet = dialog.build_result().bet
    assert bet.legs[0].odds == Decimal(2)
    assert bet.legs[0].odds_text == "evens"
    assert bet.legs[1].odds_text is None


def test_bet_dialog_backfill_and_validation(qtbot, repo: Repository, clock: FakeClock):
    dialog = BetDialog(venues=repo.list_venues(), clock=clock)
    qtbot.addWidget(dialog)
    fill_f1(dialog)
    dialog.backfill.setChecked(True)
    dialog.backfill_outcome.setCurrentIndex(1)  # lay won
    bet = dialog.build_result().bet
    assert [leg.result for leg in bet.legs] == [LegResult.LOST, LegResult.WON]
    assert bet.settled_at == clock.now
    created = repo.create_bet(bet)
    assert created.actual_pl_pence == -57
    dialog.back_odds.setText("1.0")
    assert not dialog.save_button.isEnabled()
    assert "greater than 1" in dialog.error_label.text()
    dialog.back_odds.setText("2.0")
    dialog.event_name.setText("")
    assert not dialog.save_button.isEnabled()


def test_bet_dialog_prefill_edit_and_correct(qtbot, repo: Repository, clock: FakeClock):
    created = repo.create_bet(f1_bet(notes="hello"))
    dialog = BetDialog(venues=repo.list_venues(), mode=BetDialogMode.EDIT, prefill=created)
    qtbot.addWidget(dialog)
    assert dialog.event_name.text() == "Arsenal v Chelsea"
    assert dialog.bookmaker.currentText() == "Bet365"
    assert dialog.lay_stake.text() == "9.62"
    assert dialog.notes.toPlainText() == "hello"
    assert not dialog.backfill.isVisible()
    assert dialog.save_button.isEnabled()
    assert dialog.build_result().reason is None

    repo.settle_two_way(created.id, TwoWayOutcome.BACK_WON)
    correct = BetDialog(
        venues=repo.list_venues(), mode=BetDialogMode.CORRECT, prefill=repo.get_bet(created.id)
    )
    qtbot.addWidget(correct)
    assert not correct.save_button.isEnabled()  # reason missing
    correct.reason.setText("typo in the stake")
    assert correct.save_button.isEnabled()
    assert correct.build_result().reason == "typo in the stake"


def test_bet_dialog_settings_roundtrip(qtbot, repo: Repository, ui_settings, clock):
    dialog = BetDialog(venues=repo.list_venues(), settings=ui_settings, clock=clock)
    qtbot.addWidget(dialog)
    fill_f1(dialog)
    dialog.save_button.click()
    assert dialog.result_value is not None
    assert ui_settings.last_exchange == "Smarkets"
    assert ui_settings.last_commission_bp == 200
    again = BetDialog(venues=repo.list_venues(), settings=ui_settings, clock=clock)
    qtbot.addWidget(again)
    assert again.exchange.currentText() == "Smarkets"
    assert again.bookmaker.currentText() == "Bet365"
    assert again.commission.text() == "2"


def test_bet_dialog_rejects_non_two_way_prefill(qtbot, repo: Repository):
    dutch = repo.create_bet(
        NewBet(
            BetType.DUTCHING,
            "Three-way",
            market_outcomes=3,
            legs=[
                NewLeg(Side.BACK, "Bet365", Decimal("2.5"), 1200, selection=0),
                NewLeg(Side.BACK, "Coral", Decimal("3.4"), 882, selection=1),
                NewLeg(Side.BACK, "Betfred", Decimal("3.1"), 968, selection=2),
            ],
        )
    )
    with pytest.raises(ValidationError):
        BetDialog(venues=repo.list_venues(), prefill=dutch)


def test_settle_dialog_buttons_and_per_leg(qtbot, repo: Repository, clock: FakeClock):
    bet = repo.create_bet(f1_bet())
    dialog = SettleDialog(bet, clock=clock)
    qtbot.addWidget(dialog)
    assert set(dialog.outcome_buttons) == set(TwoWayOutcome)
    dialog.outcome_buttons[TwoWayOutcome.LAY_WON].click()
    assert dialog.result_value is not None
    assert dialog.result_value.outcome is TwoWayOutcome.LAY_WON
    assert dialog.result_value.settled_at == clock.now

    per_leg = SettleDialog(bet, clock=clock)
    qtbot.addWidget(per_leg)
    back_row, lay_row = per_leg.rows
    back_row.result.setCurrentIndex(
        list(back_row.result.itemData(i) for i in range(5)).index(LegResult.CASHED_OUT)
    )
    assert back_row.amount.isEnabled()
    back_row.amount.setText("20.00")  # the £20 return on a £10 cash stake
    lay_row.result.setCurrentIndex(1)  # WON
    assert per_leg.per_leg_results() == {0: (LegResult.CASHED_OUT, 1000), 1: (LegResult.WON, None)}
    per_leg._apply()
    assert per_leg.result_value is not None
    updated = repo.settle(
        bet.id, per_leg.result_value.per_leg, settled_at=per_leg.result_value.settled_at
    )
    assert updated.actual_pl_pence == 1943  # F14a


def test_settle_dialog_hides_buttons_for_partially_settled(qtbot, repo: Repository):
    bet = repo.create_bet(f1_bet())
    repo.settle(bet.id, {0: (LegResult.CASHED_OUT, 1000)})
    dialog = SettleDialog(repo.get_bet(bet.id))
    qtbot.addWidget(dialog)
    assert dialog.outcome_buttons == {}
    assert len(dialog.rows) == 1
    dialog._apply()
    assert dialog.result_value is None
    assert "at least one leg" in dialog.error_label.text()


def current_result(dialog: AdjustDialog):  # defeats mypy attribute narrowing
    return dialog.result_value


def test_adjust_dialog(qtbot, repo: Repository):
    bet = repo.create_bet(f1_bet())
    repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    dialog = AdjustDialog(repo.get_bet(bet.id))
    qtbot.addWidget(dialog)
    assert not dialog.clear_radio.isEnabled()
    dialog.amount.setText("0")
    dialog._accept()
    assert current_result(dialog) is None  # reason missing
    dialog.reason.setText("2-up paid out")
    dialog._accept()
    request = current_result(dialog)
    assert request is not None
    assert (request.override, request.reason) == (0, "2-up paid out")
    adjusted = repo.adjust(bet.id, 0, "2-up paid out")
    clearing = AdjustDialog(adjusted)
    qtbot.addWidget(clearing)
    assert clearing.clear_radio.isEnabled()
    clearing.clear_radio.setChecked(True)
    clearing.reason.setText("wrong bet")
    assert clearing.build_request().override is None


def test_stake_kind_for_each_type():
    assert stake_kind_for(BetType.QUALIFYING) is StakeKind.CASH
    assert stake_kind_for(BetType.FREE_BET_SNR) is StakeKind.FREE_SNR
    assert stake_kind_for(BetType.FREE_BET_SR) is StakeKind.FREE_SR
    assert VenueKind.EXCHANGE.value == "EXCHANGE"
