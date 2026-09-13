from __future__ import annotations

from decimal import Decimal

import pytest

from odds_pup.core import (
    MAX_MONEY_PENCE,
    CoreError,
    Leg,
    LegResult,
    OddsError,
    Side,
    StakeKind,
    ValidationError,
)

D = Decimal


def test_leg_defaults():
    leg = Leg(Side.BACK, "bookie", D("2.0"), 1000)
    assert leg.selection == 0
    assert leg.stake_kind is StakeKind.CASH
    assert leg.commission_bp == 0
    assert leg.result is LegResult.PENDING
    assert not leg.is_settled
    assert not leg.is_free


@pytest.mark.parametrize(
    "kwargs",
    [
        {"venue": ""},
        {"venue": "   "},
        {"stake": -1},
        {"stake": 10.0},
        {"stake": True},
        {"selection": -1},
        {"commission_bp": -1},
        {"commission_bp": 10000},
        {"commission_bp": 2.0},
        {"result": LegResult.CASHED_OUT},
        {"result": LegResult.WON, "settled_amount": 5},
        {"result": LegResult.CASHED_OUT, "settled_amount": 10.0},
        {"result": LegResult.CASHED_OUT, "settled_amount": True},
        {"result": LegResult.CASHED_OUT, "settled_amount": MAX_MONEY_PENCE + 1},
        {"selection": True},
        {"selection": 1.0},
        {"stake": MAX_MONEY_PENCE + 1},
        {"venue": 42},
    ],
)
def test_leg_rejects(kwargs):
    base = {"side": Side.BACK, "venue": "bookie", "odds": D("2.0"), "stake": 1000}
    with pytest.raises(ValidationError):
        Leg(**{**base, **kwargs})


def test_leg_rejects_bad_odds():
    with pytest.raises(OddsError):
        Leg(Side.BACK, "bookie", D("1.0"), 1000)


def test_lay_leg_cannot_be_free():
    with pytest.raises(ValidationError):
        Leg(Side.LAY, "exchange", D("2.0"), 1000, stake_kind=StakeKind.FREE_SNR)


def test_liability():
    assert Leg(Side.LAY, "exchange", D("2.10"), 962).liability == 1058
    assert Leg(Side.BACK, "bookie", D("2.10"), 962).liability == 962
    assert Leg(Side.BACK, "bookie", D("2.10"), 962, stake_kind=StakeKind.FREE_SNR).liability == 0
    assert Leg(Side.BACK, "bookie", D("2.10"), 962, stake_kind=StakeKind.FREE_SR).liability == 0


def test_settled_and_reopened_copies():
    leg = Leg(Side.BACK, "bookie", D("2.0"), 1000)
    cashed = leg.settled(LegResult.CASHED_OUT, 1000)
    assert cashed.is_settled
    assert cashed.settled_amount == 1000
    assert cashed.reopened() == leg
    with pytest.raises(ValidationError):
        leg.settled(LegResult.CASHED_OUT)


def test_cashed_out_from_return_nets_off_cash_stake():
    cash = Leg(Side.BACK, "bookie", D("2.0"), 1000)
    assert cash.cashed_out_from_return(2000).settled_amount == 1000  # F14: 2-up early payout
    assert cash.cashed_out_from_return(0).settled_amount == -1000  # cashed out for nothing
    assert cash.cashed_out_from_return(1550).settled_amount == 550
    snr = Leg(Side.BACK, "bookie", D("2.0"), 1000, stake_kind=StakeKind.FREE_SNR)
    assert snr.cashed_out_from_return(750).settled_amount == 750
    sr = Leg(Side.BACK, "bookie", D("2.0"), 1000, stake_kind=StakeKind.FREE_SR)
    assert sr.cashed_out_from_return(1800).settled_amount == 1800
    with pytest.raises(ValidationError):
        cash.cashed_out_from_return(-1)
    with pytest.raises(ValidationError):
        cash.cashed_out_from_return(10.0)  # type: ignore[arg-type]


def test_cashed_out_on_lay_takes_net_only():
    lay = Leg(Side.LAY, "exchange", D("2.1"), 962, commission_bp=200)
    assert lay.cashed_out(-350).settled_amount == -350
    with pytest.raises(ValidationError):
        lay.cashed_out_from_return(500)


def test_venue_is_trimmed_and_key_is_casefolded():
    leg = Leg(Side.BACK, "  Bet365 ", D("2.0"), 1000)
    assert leg.venue == "Bet365"
    assert leg.venue_key == "bet365"
    assert Leg(Side.BACK, "BET365", D("2.0"), 1000).venue_key == leg.venue_key


def test_bad_odds_are_core_errors_too():
    with pytest.raises(CoreError):
        Leg(Side.BACK, "bookie", D("1.0"), 1000)
