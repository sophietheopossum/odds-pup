"""Golden fixtures from docs/SPEC.md §7. Every value is exact pence.

If one of these fails, either the spec or the code is wrong; recompute by hand from SPEC §4-§6
before touching either.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from odds_pup.core import (
    BetStatus,
    BetType,
    Leg,
    LegResult,
    OddsError,
    Side,
    StakeKind,
    book_percentage,
    dutch_stakes,
    dutch_stakes_from_free_leg,
    guaranteed_pl,
    outcome_pl,
    parse_odds,
    pl_vector,
    realised_pl,
    status_of,
    two_way_position,
)

D = Decimal

# §7.1: id, type, S, B, O, bp, L*, liability, back wins, lay wins, guaranteed, rating
TWO_WAY_OPTIMAL = [
    ("F1", BetType.QUALIFYING, 1000, "2.00", "2.10", 200, 962, 1058, -58, -57, -58, "94.23"),
    ("F2", BetType.FREE_BET_SNR, 1000, "2.00", "2.10", 200, 481, 529, 471, 471, 471, "47.12"),
    ("F3", BetType.FREE_BET_SR, 1000, "2.00", "2.10", 200, 962, 1058, 942, 943, 942, "94.23"),
    ("F4", BetType.FREE_BET_SNR, 2000, "6.0", "6.4", 200, 1567, 8462, 1538, 1536, 1536, "76.80"),
    ("F5", BetType.QUALIFYING, 2500, "3.5", "3.5", 0, 2500, 6250, 0, 0, 0, "100.00"),
    ("F8", BetType.QUALIFYING, 5000, "4.3333", "4.4", 500, 4981, 16935, -268, -268, -268, "94.64"),
    ("F9", BetType.FREE_BET_SNR, 500, "11.0", "12.0", 200, 417, 4587, 413, 409, 409, "81.80"),
    ("F10", BetType.QUALIFYING, 1000, "1.50", "1.52", 200, 1000, 520, -20, -20, -20, "98.00"),
    ("F11", BetType.FREE_BET_SR, 2500, "3.0", "3.2", 0, 2344, 5157, 2343, 2344, 2343, "93.75"),
]


@pytest.mark.parametrize(
    ("bet_type", "s", "b", "o", "bp", "lay", "liab", "back_wins", "lay_wins", "guaranteed", "rate"),
    [row[1:] for row in TWO_WAY_OPTIMAL],
    ids=[row[0] for row in TWO_WAY_OPTIMAL],
)
def test_two_way_with_suggested_lay_stake(
    bet_type, s, b, o, bp, lay, liab, back_wins, lay_wins, guaranteed, rate
):
    pos = two_way_position(bet_type, s, D(b), D(o), bp)
    assert pos.suggested_lay_stake == lay
    assert pos.lay.stake == lay
    assert pos.liability == liab
    assert pos.pl_if_back_wins == back_wins
    assert pos.pl_if_lay_wins == lay_wins
    assert pos.guaranteed == guaranteed
    assert str(pos.rating) == rate  # exact 2-dp form, as displayed
    assert pos.lay_stake_is_suggested


# §7.2: user overrode the suggested lay stake
@pytest.mark.parametrize(
    ("lay", "liab", "back_wins", "lay_wins", "guaranteed"),
    [(962, 1058, -58, -57, -58), (1000, 1100, -100, -20, -100)],
    ids=["F6", "F7"],
)
def test_two_way_with_given_lay_stake(lay, liab, back_wins, lay_wins, guaranteed):
    pos = two_way_position(BetType.QUALIFYING, 1000, D("2.00"), D("2.10"), 200, lay_stake=lay)
    assert pos.suggested_lay_stake == 962
    assert pos.lay.stake == lay
    assert pos.lay_stake_is_suggested == (lay == 962)
    assert pos.liability == liab
    assert (pos.pl_if_back_wins, pos.pl_if_lay_wins, pos.guaranteed) == (
        back_wins,
        lay_wins,
        guaranteed,
    )


def test_f12_dutching_three_way():
    odds = [D("2.50"), D("3.40"), D("3.10")]
    stakes = dutch_stakes(odds, 3000)
    assert stakes == (1200, 882, 968)
    assert sum(stakes) == 3050
    legs = [Leg(Side.BACK, f"bookmaker-{i}", odds[i], stakes[i], selection=i) for i in range(3)]
    assert pl_vector(legs, 3) == (-50, -51, -49)
    assert guaranteed_pl(legs, 3) == -51
    assert book_percentage(odds) == D("101.67")


def test_f12b_dutching_around_a_free_bet():
    odds = [D("3.0"), D("3.4"), D("3.1")]
    stakes = dutch_stakes_from_free_leg(odds, 0, 1000)
    assert stakes == (1000, 588, 645)
    legs = [
        Leg(Side.BACK, "bookmaker-0", odds[0], 1000, selection=0, stake_kind=StakeKind.FREE_SNR),
        Leg(Side.BACK, "bookmaker-1", odds[1], 588, selection=1),
        Leg(Side.BACK, "bookmaker-2", odds[2], 645, selection=2),
    ]
    assert pl_vector(legs, 3) == (767, 766, 767)
    assert guaranteed_pl(legs, 3) == 766


def _f1_legs() -> tuple[Leg, Leg]:
    pos = two_way_position(BetType.QUALIFYING, 1000, D("2.00"), D("2.10"), 200)
    return pos.back, pos.lay


# §7.4: settlement of F1
@pytest.mark.parametrize(
    ("back_result", "back_amount", "lay_result", "expected"),
    [
        (LegResult.WON, None, LegResult.LOST, -58),
        (LegResult.LOST, None, LegResult.WON, -57),
        (LegResult.VOID, None, LegResult.VOID, 0),
        (LegResult.VOID, None, LegResult.WON, 943),
        (LegResult.VOID, None, LegResult.LOST, -1058),
        (LegResult.CASHED_OUT, 1000, LegResult.WON, 1943),
        (LegResult.CASHED_OUT, 1000, LegResult.LOST, -58),
    ],
    ids=["F13a", "F13b", "F13c", "F13d", "F13e", "F14a", "F14b"],
)
def test_settlement_of_f1(back_result, back_amount, lay_result, expected):
    back, lay = _f1_legs()
    if back_result is LegResult.CASHED_OUT:
        # the editor is given the £20.00 return the bookmaker credited, not the net figure
        settled_back = back.cashed_out_from_return(back_amount + back.stake)
        assert settled_back.settled_amount == back_amount
    else:
        settled_back = back.settled(back_result, back_amount)
    settled = (settled_back, lay.settled(lay_result))
    assert realised_pl(settled) == expected
    # every outcome agrees once all legs are constants
    assert set(pl_vector(settled, 2)) == {expected}


def test_f16_commission_tie_rounds_half_up():
    lay = Leg(Side.LAY, "exchange", D("2.00"), 975, commission_bp=200, result=LegResult.WON)
    assert outcome_pl([lay], 0) == 955
    # F16b: 925 * 2% = 18.5 -> 19 under HALF_UP (HALF_EVEN would give 18 and net 907)
    lay_b = Leg(Side.LAY, "exchange", D("2.00"), 925, commission_bp=200, result=LegResult.WON)
    assert outcome_pl([lay_b], 0) == 906


def test_spec_16_1_lock_in_after_early_payout():
    back, lay = _f1_legs()
    paid = back.settled(LegResult.WON)  # bookmaker paid the back out as a winner
    lock_in = Leg(Side.BACK, lay.venue, D("1.50"), 1347, commission_bp=200)
    assert pl_vector([lay, lock_in]) == (-384, -385)
    assert pl_vector([paid, lay, lock_in]) == (616, 615)
    assert status_of([paid, lay, lock_in]) is BetStatus.PARTIALLY_SETTLED


# §7.6: odds parsing
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("11/8", "2.375"),
        ("100/30", "4.3333"),
        ("1/1", "2"),
        ("evens", "2"),
        ("EVS", "2"),
        ("Evens", "2"),
        ("2.1", "2.1"),
    ],
)
def test_parse_odds_accepts(text, expected):
    assert parse_odds(text) == D(expected)


@pytest.mark.parametrize("text", ["1.00", "0/1", "2,10", "2.12345", "abc", "", "1", "3/0", "-2.0"])
def test_parse_odds_rejects(text):
    with pytest.raises(OddsError):
        parse_odds(text)
