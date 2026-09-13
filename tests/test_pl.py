from __future__ import annotations

from decimal import Decimal

import pytest

from odds_pup.core import (
    BetStatus,
    CoreError,
    Leg,
    LegResult,
    Side,
    StakeKind,
    ValidationError,
    guaranteed_pl,
    leg_gross,
    open_liability,
    outcome_pl,
    pl_vector,
    realised_pl,
    status_of,
    validate_position,
)

D = Decimal
BACK = Leg(Side.BACK, "bookie", D("2.00"), 1000)
LAY = Leg(Side.LAY, "exchange", D("2.10"), 962, commission_bp=200)


def test_leg_gross_table_back_cash():
    assert leg_gross(BACK, 0) == 1000
    assert leg_gross(BACK, 1) == -1000


def test_leg_gross_table_back_free():
    snr = Leg(Side.BACK, "bookie", D("2.00"), 1000, stake_kind=StakeKind.FREE_SNR)
    sr = Leg(Side.BACK, "bookie", D("2.00"), 1000, stake_kind=StakeKind.FREE_SR)
    assert (leg_gross(snr, 0), leg_gross(snr, 1)) == (1000, 0)
    assert (leg_gross(sr, 0), leg_gross(sr, 1)) == (2000, 0)


def test_leg_gross_table_lay():
    assert leg_gross(LAY, 0) == -1058
    assert leg_gross(LAY, 1) == 962


def test_settled_legs_are_constants():
    won = BACK.settled(LegResult.WON)
    lost = BACK.settled(LegResult.LOST)
    void = BACK.settled(LegResult.VOID)
    cashed = BACK.settled(LegResult.CASHED_OUT, 750)
    for outcome in (0, 1, 7):
        assert leg_gross(won, outcome) == 1000
        assert leg_gross(lost, outcome) == -1000
        assert leg_gross(void, outcome) == 0
        assert leg_gross(cashed, outcome) == 750
    lay_won, lay_lost = LAY.settled(LegResult.WON), LAY.settled(LegResult.LOST)
    assert leg_gross(lay_won, 0) == 962
    assert leg_gross(lay_lost, 1) == -1058


def test_commission_only_on_positive_exchange_net():
    assert outcome_pl([LAY], 0) == -1058  # lay loses: no commission
    assert outcome_pl([LAY], 1) == 962 - 19  # lay wins: 2% of 962 = 19.24 -> 19


def test_commission_is_netted_per_venue_across_legs():
    # Two exchange legs in one market: a lay that wins 962 and a back that loses 500 net to +462.
    ex_back = Leg(Side.BACK, "exchange", D("1.50"), 500, commission_bp=200)
    legs = [LAY, ex_back]
    # outcome 1: selection loses -> lay +962, back -500 -> net 462 -> commission 9.24 -> 9
    assert outcome_pl(legs, 1) == 462 - 9
    # outcome 0: selection wins -> lay -1058, back +250 -> net -808 -> no commission
    assert outcome_pl(legs, 0) == -808


def test_cashed_out_amount_is_excluded_from_commission_base():
    cashed = Leg(Side.BACK, "exchange", D("1.50"), 500, commission_bp=200).settled(
        LegResult.CASHED_OUT, 300
    )
    # lay wins 962 -> commission on 962 only (19), plus 300 already-net
    assert outcome_pl([LAY, cashed], 1) == 943 + 300


def test_pl_vector_and_guaranteed():
    assert pl_vector([BACK, LAY]) == (-58, -57)
    assert guaranteed_pl([BACK, LAY]) == -58


def test_realised_is_none_while_pending():
    assert realised_pl([BACK, LAY]) is None
    assert realised_pl([BACK.settled(LegResult.WON), LAY]) is None
    assert realised_pl([]) is None
    assert realised_pl([BACK.settled(LegResult.WON), LAY.settled(LegResult.LOST)]) == -58


def test_status_of():
    assert status_of([BACK, LAY]) is BetStatus.OPEN
    assert status_of([BACK.settled(LegResult.WON), LAY]) is BetStatus.PARTIALLY_SETTLED
    assert (
        status_of([BACK.settled(LegResult.WON), LAY.settled(LegResult.LOST)]) is BetStatus.SETTLED
    )
    assert status_of([BACK.settled(LegResult.VOID), LAY.settled(LegResult.VOID)]) is BetStatus.VOID
    assert (
        status_of([BACK.settled(LegResult.VOID), LAY.settled(LegResult.WON)]) is BetStatus.SETTLED
    )
    assert status_of([BACK.settled(LegResult.CASHED_OUT, 0)]) is BetStatus.SETTLED
    with pytest.raises(ValidationError):
        status_of([])


def test_open_liability_counts_only_pending_lays():
    assert open_liability([BACK, LAY]) == 1058
    assert open_liability([BACK, LAY.settled(LegResult.WON)]) == 0
    assert open_liability([BACK]) == 0


def test_validate_position_rejects_bad_positions():
    with pytest.raises(ValidationError):
        validate_position([])
    with pytest.raises(ValidationError):
        validate_position([BACK], market_outcomes=1)
    with pytest.raises(ValidationError):
        validate_position([Leg(Side.BACK, "bookie", D("2.0"), 100, selection=2)], 2)
    with pytest.raises(ValidationError):
        validate_position(
            [LAY, Leg(Side.LAY, "exchange", D("2.0"), 100, commission_bp=500)],
        )


def test_partial_dutch_uncovered_outcome_loses_everything():
    legs = [
        Leg(Side.BACK, "a", D("3.0"), 1000, selection=0),
        Leg(Side.BACK, "b", D("3.0"), 1000, selection=1),
    ]
    assert pl_vector(legs, 3) == (1000, 1000, -2000)
    assert guaranteed_pl(legs, 3) == -2000


def test_mismatched_commission_in_one_venue_is_rejected_in_every_order():
    a = Leg(Side.LAY, "exchange", D("2.10"), 962, commission_bp=200, result=LegResult.WON)
    b = Leg(Side.LAY, "Exchange ", D("3.00"), 500, commission_bp=500, result=LegResult.WON)
    for legs in ([a, b], [b, a]):
        with pytest.raises(ValidationError):
            realised_pl(legs)
        with pytest.raises(ValidationError):
            outcome_pl(legs, 0)
        with pytest.raises(ValidationError):
            pl_vector(legs)


def test_realised_pl_is_order_independent():
    a = Leg(Side.LAY, "exchange", D("2.10"), 962, commission_bp=200, result=LegResult.WON)
    b = Leg(Side.BACK, "exchange", D("1.50"), 500, commission_bp=200, result=LegResult.LOST)
    c = BACK.settled(LegResult.WON)
    assert realised_pl([a, b, c]) == realised_pl([c, b, a]) == realised_pl([b, c, a])


def test_venue_grouping_ignores_case_and_whitespace():
    lay = Leg(Side.LAY, "Betfair", D("2.10"), 962, commission_bp=200)
    back = Leg(Side.BACK, " betfair ", D("1.50"), 500, commission_bp=200)
    assert back.venue == "betfair"
    assert back.venue_key == lay.venue_key
    # one group: outcome 1 nets +962 - 500 = 462 -> commission 9 -> 453
    assert outcome_pl([lay, back], 1) == 453


def test_zero_net_exchange_group_takes_no_commission():
    lay = Leg(Side.LAY, "exchange", D("2.00"), 500, commission_bp=200)
    back = Leg(Side.BACK, "exchange", D("2.00"), 500, commission_bp=200)
    assert pl_vector([lay, back]) == (0, 0)


def test_lay_on_selection_one_in_a_three_way_market():
    lay = Leg(Side.LAY, "exchange", D("2.10"), 962, commission_bp=200, selection=1)
    assert pl_vector([lay], 3) == (943, -1058, 943)


def test_negative_cash_out_flows_through_realised_pl():
    legs = [BACK.cashed_out_from_return(0), LAY.settled(LegResult.WON)]
    assert realised_pl(legs) == -1000 + 943
    legs = [BACK.cashed_out(-250), LAY.settled(LegResult.LOST)]
    assert realised_pl(legs) == -250 - 1058


def test_market_outcomes_must_be_a_real_int():
    with pytest.raises(ValidationError):
        validate_position([BACK], market_outcomes=True)


def test_every_domain_error_is_a_core_error():
    with pytest.raises(CoreError):
        validate_position([])
