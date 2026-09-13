from __future__ import annotations

from decimal import Decimal

import pytest

from odds_pup.core import (
    BetType,
    Leg,
    LegResult,
    Side,
    TwoWayOutcome,
    ValidationError,
    book_percentage,
    dutch_stakes,
    dutch_stakes_from_free_leg,
    exact_rating,
    optimal_lay_stake,
    pl_vector,
    rating,
    realised_pl,
    settle_two_way,
    status_of,
    two_way_position,
)

D = Decimal


def test_optimal_lay_stake_closed_forms():
    # S*B/(O-c) for QL and SR, S*(B-1)/(O-c) for SNR
    assert optimal_lay_stake(BetType.QUALIFYING, 1000, D("2.00"), D("2.10"), 200) == 962
    assert optimal_lay_stake(BetType.FREE_BET_SR, 1000, D("2.00"), D("2.10"), 200) == 962
    assert optimal_lay_stake(BetType.FREE_BET_SNR, 1000, D("2.00"), D("2.10"), 200) == 481


def test_optimal_lay_stake_rejects_dutching():
    with pytest.raises(ValidationError):
        optimal_lay_stake(BetType.DUTCHING, 1000, D("2.00"), D("2.10"), 200)


def test_rating_formulas():
    assert rating(BetType.QUALIFYING, D("2.00"), D("2.10"), 200) == D("94.23")
    assert rating(BetType.FREE_BET_SR, D("2.00"), D("2.10"), 200) == D("94.23")
    assert rating(BetType.FREE_BET_SNR, D("2.00"), D("2.10"), 200) == D("47.12")
    assert str(rating(BetType.QUALIFYING, D("3.5"), D("3.5"), 0)) == "100.00"


def test_exact_rating_is_unrounded():
    precise = exact_rating(BetType.QUALIFYING, D("2.00"), D("2.10"), 200)
    assert D("94.23") < precise < D("94.2308")
    assert exact_rating(BetType.QUALIFYING, D("2.00"), D("2.50"), 0) == 80


def test_two_way_position_rejects_bad_input():
    with pytest.raises(ValidationError):
        two_way_position(BetType.DUTCHING, 1000, D("2.0"), D("2.1"), 200)
    with pytest.raises(ValidationError):
        two_way_position(BetType.QUALIFYING, 0, D("2.0"), D("2.1"), 200)
    with pytest.raises(ValidationError):
        two_way_position(BetType.QUALIFYING, 1000, D("2.0"), D("2.1"), 200, lay_stake=-1)
    with pytest.raises(ValidationError):
        two_way_position(
            BetType.QUALIFYING, 1000, D("2.0"), D("2.1"), 200, bookmaker="Bet X", exchange=" bet x "
        )


def test_two_way_position_carries_venues_and_odds_text():
    pos = two_way_position(
        BetType.FREE_BET_SNR,
        1000,
        D("2.375"),
        D("2.4"),
        200,
        bookmaker="Bet365",
        exchange="Smarkets",
        back_odds_text="11/8",
    )
    assert pos.back.venue == "Bet365"
    assert pos.lay.venue == "Smarkets"
    assert pos.back.odds_text == "11/8"
    assert pos.lay.odds_text is None
    assert pos.legs == (pos.back, pos.lay)


def test_settle_two_way_matches_vector():
    pos = two_way_position(BetType.QUALIFYING, 1000, D("2.00"), D("2.10"), 200)
    back_won = settle_two_way(pos.back, pos.lay, TwoWayOutcome.BACK_WON)
    lay_won = pos.settled(TwoWayOutcome.LAY_WON)
    void = pos.settled(TwoWayOutcome.VOID)
    assert realised_pl(back_won) == pos.pl_if_back_wins
    assert realised_pl(lay_won) == pos.pl_if_lay_wins
    assert realised_pl(void) == 0
    assert [leg.result for leg in back_won] == [LegResult.WON, LegResult.LOST]
    assert [leg.result for leg in lay_won] == [LegResult.LOST, LegResult.WON]
    assert status_of(void).value == "VOID"


def test_settle_two_way_refuses_wrong_sides_or_settled_legs():
    pos = two_way_position(BetType.QUALIFYING, 1000, D("2.00"), D("2.10"), 200)
    with pytest.raises(ValidationError):
        settle_two_way(pos.lay, pos.back, TwoWayOutcome.BACK_WON)
    paid_early = pos.back.cashed_out_from_return(2000)
    with pytest.raises(ValidationError):
        settle_two_way(paid_early, pos.lay, TwoWayOutcome.LAY_WON)
    # the per-leg route still works for that case
    assert realised_pl([paid_early, pos.lay.settled(LegResult.WON)]) == 1943


def test_dutch_stakes_and_book():
    assert dutch_stakes([D("2.0"), D("2.0")], 1000) == (500, 500)
    assert book_percentage([D("2.0"), D("2.0")]) == D("100.00")
    assert book_percentage([D("2.1"), D("2.1")]) == D("95.24")
    with pytest.raises(ValidationError):
        dutch_stakes([D("2.0")], 1000)
    with pytest.raises(ValidationError):
        dutch_stakes([D("2.0"), D("3.0")], 0)


def test_book_percentage_rounds_once_and_is_order_independent():
    # 1/1.2 + 1/12 + 1/19.2 = 93/96 = 0.96875 exactly -> 96.875% -> HALF_UP 96.88
    prices = [D("1.2"), D("12.0"), D("19.2")]
    assert book_percentage(prices) == D("96.88")
    assert book_percentage(list(reversed(prices))) == D("96.88")
    assert str(book_percentage([D("4.0"), D("4.0"), D("2.0")])) == "100.00"


def test_dutch_stakes_from_free_leg():
    odds = [D("3.0"), D("3.4"), D("3.1")]
    stakes = dutch_stakes_from_free_leg(odds, 0, 1000)
    assert stakes == (1000, 588, 645)
    legs = [
        Leg(Side.BACK, "a", odds[0], stakes[0], selection=0, stake_kind="FREE_SNR"),  # type: ignore[arg-type]
        Leg(Side.BACK, "b", odds[1], stakes[1], selection=1),
        Leg(Side.BACK, "c", odds[2], stakes[2], selection=2),
    ]
    assert pl_vector(legs, 3) == (767, 766, 767)
    with pytest.raises(ValidationError):
        dutch_stakes_from_free_leg(odds, 3, 1000)
    with pytest.raises(ValidationError):
        dutch_stakes_from_free_leg(odds[:1], 0, 1000)
