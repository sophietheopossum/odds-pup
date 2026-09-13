from __future__ import annotations

from decimal import Decimal

from odds_pup.core import (
    BETFAIR,
    Advisory,
    AdvisoryCode,
    BetType,
    advisories_for,
    two_way_position,
)

D = Decimal


def codes(advisories: list[Advisory]) -> list[AdvisoryCode]:
    return [a.code for a in advisories]


def test_clean_position_has_no_advisories():
    pos = two_way_position(BetType.QUALIFYING, 1000, D("2.00"), D("2.10"), 200)
    assert advisories_for(pos, ladder=BETFAIR, min_lay_stake=200) == []


def test_off_ladder_lay_odds():
    pos = two_way_position(BetType.QUALIFYING, 1000, D("2.00"), D("2.11"), 200)
    found = advisories_for(pos, ladder=BETFAIR)
    assert codes(found) == [AdvisoryCode.LAY_ODDS_OFF_LADDER]
    assert "2.10 or 2.12" in found[0].message


def test_off_ladder_outside_the_ladder_offers_one_option():
    high = two_way_position(BetType.QUALIFYING, 1000, D("1000"), D("1200"), 200)
    assert advisories_for(high, ladder=BETFAIR)[0].message.endswith("nearest: 1000.00.")
    low = two_way_position(BetType.QUALIFYING, 1000, D("1.005"), D("1.005"), 200)
    assert advisories_for(low, ladder=BETFAIR)[0].message.endswith("nearest: 1.01.")


def test_lay_stake_below_minimum():
    pos = two_way_position(BetType.FREE_BET_SNR, 100, D("5.0"), D("5.2"), 200)
    assert pos.lay.stake < 200
    assert codes(advisories_for(pos, min_lay_stake=200)) == [AdvisoryCode.LAY_STAKE_BELOW_MINIMUM]
    unlaid = two_way_position(BetType.FREE_BET_SNR, 100, D("5.0"), D("5.2"), 200, lay_stake=0)
    assert advisories_for(unlaid, min_lay_stake=200) == []


def test_high_commission_boundary():
    at = two_way_position(BetType.FREE_BET_SNR, 1000, D("2.00"), D("2.10"), 1000)
    above = two_way_position(BetType.FREE_BET_SNR, 1000, D("2.00"), D("2.10"), 1001)
    assert advisories_for(at) == []
    found = advisories_for(above)
    assert codes(found) == [AdvisoryCode.HIGH_COMMISSION]
    assert "10.01%" in found[0].message


def test_high_commission_and_low_rating():
    pos = two_way_position(BetType.QUALIFYING, 1000, D("2.00"), D("3.00"), 1500)
    assert codes(advisories_for(pos)) == [AdvisoryCode.HIGH_COMMISSION, AdvisoryCode.LOW_RATING]


def test_low_rating_uses_the_exact_rating_not_the_display_one():
    exactly_80 = two_way_position(BetType.QUALIFYING, 1000, D("2.00"), D("2.50"), 0)
    assert advisories_for(exactly_80) == []
    just_under = two_way_position(BetType.QUALIFYING, 1000, D("2.00"), D("2.5001"), 0)
    assert just_under.rating == D("80.00")  # display rounds up...
    assert codes(advisories_for(just_under)) == [AdvisoryCode.LOW_RATING]  # ...the check does not


def test_low_rating_only_for_qualifiers():
    pos = two_way_position(BetType.FREE_BET_SNR, 1000, D("2.00"), D("2.10"), 200)
    assert pos.rating < 80
    assert advisories_for(pos) == []
