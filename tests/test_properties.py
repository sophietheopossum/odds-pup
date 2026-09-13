"""Hypothesis properties over the calculator (SPEC §6.1 identities and rounding bounds)."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from hypothesis import assume, given
from hypothesis import strategies as st

from odds_pup.core import (
    BetType,
    Leg,
    LegResult,
    Side,
    TwoWayOutcome,
    optimal_lay_stake,
    outcome_pl,
    pl_vector,
    realised_pl,
    settle_two_way,
    status_of,
    two_way_position,
)

# The spec region: stakes to £1,000,000, odds to 1000 at 2 or 4 dp, commission to 50%.
# (Commission above 50% with odds near 1 would push the suggested lay stake past MAX_MONEY_PENCE.)
stakes = st.integers(min_value=1, max_value=100_000_000)
odds = st.one_of(
    st.decimals(min_value=Decimal("1.01"), max_value=Decimal("1000"), places=2, allow_nan=False),
    st.decimals(min_value=Decimal("1.0001"), max_value=Decimal("1000"), places=4, allow_nan=False),
)
commission = st.integers(min_value=0, max_value=5000)
two_way_types = st.sampled_from([BetType.QUALIFYING, BetType.FREE_BET_SNR, BetType.FREE_BET_SR])


def half_up(value: Fraction) -> int:
    """ROUND_HALF_UP (ties away from zero) on an exact rational, independent of decimal."""
    n, d = value.numerator, value.denominator
    magnitude = (2 * abs(n) + d) // (2 * d)
    return magnitude if n >= 0 else -magnitude


@given(two_way_types, stakes, odds, odds, commission)
def test_matches_exact_fraction_oracle(bet_type, s, b, o, bp):
    """L*, both branches, liability and rating against an exact rational reimplementation."""
    back_odds, lay_odds, comm = Fraction(b), Fraction(o), Fraction(bp, 10_000)
    stake_multiplier = back_odds - 1 if bet_type is BetType.FREE_BET_SNR else back_odds
    win_multiplier = back_odds if bet_type is BetType.FREE_BET_SR else back_odds - 1
    cash_lost = s if bet_type is BetType.QUALIFYING else 0  # lay-wins bookmaker leg

    lay = half_up(s * stake_multiplier / (lay_odds - comm))
    assert optimal_lay_stake(bet_type, s, b, o, bp) == lay

    pos = two_way_position(bet_type, s, b, o, bp)
    assert pos.lay.stake == lay
    assert pos.liability == half_up(lay * (lay_odds - 1))
    assert pos.pl_if_back_wins == half_up(s * win_multiplier) + half_up(-lay * (lay_odds - 1))
    commission_charged = half_up(lay * comm) if lay > 0 and comm > 0 else 0
    assert pos.pl_if_lay_wins == -cash_lost + lay - commission_charged
    assert pos.guaranteed == min(pos.pl_if_back_wins, pos.pl_if_lay_wins)
    exact_rating = 100 * stake_multiplier * (1 - comm) / (lay_odds - comm)
    assert pos.rating == Decimal(half_up(exact_rating * 100)).scaleb(-2)


@given(stakes, odds, odds, commission)
def test_sr_equals_ql_plus_stake_at_the_same_lay_stake(s, b, o, bp):
    ql = two_way_position(BetType.QUALIFYING, s, b, o, bp)
    sr = two_way_position(BetType.FREE_BET_SR, s, b, o, bp, lay_stake=ql.lay.stake)
    assert sr.pl_if_back_wins == ql.pl_if_back_wins + s
    assert sr.pl_if_lay_wins == ql.pl_if_lay_wins + s


@given(stakes, odds)
def test_matching_odds_and_no_commission_gives_zero_qualifying_loss(s, b):
    pos = two_way_position(BetType.QUALIFYING, s, b, b, 0)
    assert pos.lay.stake == s
    assert (pos.pl_if_back_wins, pos.pl_if_lay_wins) == (0, 0)
    assert str(pos.rating) == "100.00"


@given(two_way_types, stakes, odds, odds, commission)
def test_suggested_lay_stake_equalises_branches_up_to_rounding(bet_type, s, b, o, bp):
    pos = two_way_position(bet_type, s, b, o, bp)
    # diff = -(L - L*)(O - c) + rounding noise: |L - L*| <= 0.5 and three roundings of <= 0.5p.
    bound = (o - Decimal(bp).scaleb(-4)) / 2 + Decimal("1.5")
    assert abs(pos.pl_if_back_wins - pos.pl_if_lay_wins) <= bound


@given(stakes, odds, odds, commission)
def test_snr_lay_stake_never_exceeds_qualifying_lay_stake(s, b, o, bp):
    assert optimal_lay_stake(BetType.FREE_BET_SNR, s, b, o, bp) <= optimal_lay_stake(
        BetType.QUALIFYING, s, b, o, bp
    )


@given(two_way_types, stakes, odds, odds, commission, stakes, stakes)
def test_more_lay_stake_moves_branches_monotonically(bet_type, s, b, o, bp, l1, l2):
    assume(l1 < l2)
    low = two_way_position(bet_type, s, b, o, bp, lay_stake=l1)
    high = two_way_position(bet_type, s, b, o, bp, lay_stake=l2)
    assert high.pl_if_back_wins <= low.pl_if_back_wins
    assert high.pl_if_lay_wins >= low.pl_if_lay_wins


@given(two_way_types, stakes, odds, odds, commission, st.sampled_from(list(TwoWayOutcome)))
def test_settlement_matches_the_predicted_branch(bet_type, s, b, o, bp, outcome):
    pos = two_way_position(bet_type, s, b, o, bp)
    settled = settle_two_way(pos.back, pos.lay, outcome)
    expected = {
        TwoWayOutcome.BACK_WON: pos.pl_if_back_wins,
        TwoWayOutcome.LAY_WON: pos.pl_if_lay_wins,
        TwoWayOutcome.VOID: 0,
    }[outcome]
    assert realised_pl(settled) == expected
    assert realised_pl(list(reversed(settled))) == expected
    assert status_of(settled).value == ("VOID" if outcome is TwoWayOutcome.VOID else "SETTLED")
    assert pos.guaranteed == min(pl_vector(pos.legs))


@given(two_way_types, stakes, odds, odds, commission)
def test_selection_index_one_mirrors_selection_zero(bet_type, s, b, o, bp):
    pos = two_way_position(bet_type, s, b, o, bp)
    mirrored = [
        Leg(leg.side, leg.venue, leg.odds, leg.stake, 1, leg.stake_kind, leg.commission_bp)
        for leg in pos.legs
    ]
    assert pl_vector(mirrored) == tuple(reversed(pl_vector(pos.legs)))


@given(stakes, odds, commission)
def test_commission_never_charged_when_the_lay_loses(lay_stake, o, bp):
    taxed = Leg(Side.LAY, "exchange", o, lay_stake, commission_bp=bp)
    free = Leg(Side.LAY, "exchange", o, lay_stake, commission_bp=0)
    assert outcome_pl([taxed], 0) == outcome_pl([free], 0) == -taxed.liability
    assert outcome_pl([taxed], 1) <= outcome_pl([free], 1) == lay_stake


@given(stakes, odds, commission)
def test_void_lay_contributes_nothing(lay_stake, o, bp):
    void = Leg(Side.LAY, "exchange", o, lay_stake, commission_bp=bp).settled(LegResult.VOID)
    assert pl_vector([void]) == (0, 0)
