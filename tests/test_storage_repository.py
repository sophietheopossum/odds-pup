from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise

import pytest

from odds_pup.core import (
    AuditKind,
    BetStatus,
    BetType,
    LegResult,
    Side,
    TwoWayOutcome,
    ValidationError,
    VenueKind,
)
from odds_pup.storage import (
    BetFilter,
    IllegalActionError,
    NewBet,
    NewLeg,
    NotFoundError,
    Repository,
    StorageError,
)
from tests.storage_helpers import FakeClock, f1_bet, snr_bet

D = Decimal


def kinds(repo: Repository, bet_id: str) -> list[str]:
    return [f"{row.kind.value}:{row.field}" for row in repo.audit_trail(bet_id)]


# ------------------------------------------------------------------ create


def test_create_two_way_bet(repo: Repository, clock: FakeClock):
    bet = repo.create_bet(f1_bet())
    assert bet.status is BetStatus.OPEN
    assert bet.expected_pl_pence == -58
    assert bet.actual_pl_pence is None
    assert bet.settled_at is None
    assert bet.placed_at == clock.now
    assert bet.currency == "GBP"
    assert bet.bookmaker == "Bet365"
    assert bet.exchange == "Smarkets"
    assert bet.live_pl_vector == (-58, -57)
    assert bet.open_liability == 1058
    assert not bet.has_adjustments
    assert kinds(repo, bet.id) == ["CREATED:None"]
    assert repo.audit_trail(bet.id)[0].new_value is None


def test_unknown_venue_is_created_with_the_right_kind(repo: Repository):
    bet = repo.create_bet(
        f1_bet(
            legs=[
                NewLeg(Side.BACK, "  Brand New Bookie ", D("2.00"), 1000),
                NewLeg(Side.LAY, "New Exchange", D("2.10"), 962, commission_bp=200),
            ]
        )
    )
    bookie = repo.find_venue("brand new bookie")
    exchange = repo.find_venue("NEW EXCHANGE")
    assert bookie is not None
    assert bookie.kind is VenueKind.BOOKMAKER
    assert exchange is not None
    assert exchange.kind is VenueKind.EXCHANGE
    assert bet.legs[0].venue_id == bookie.id
    assert bet.legs[0].leg.venue == "Brand New Bookie"


def test_commission_defaults_to_the_exchange_default(repo: Repository):
    bet = repo.create_bet(
        f1_bet(
            legs=[
                NewLeg(Side.BACK, "Bet365", D("2.00"), 1000),
                NewLeg(Side.LAY, "Betfair Exchange", D("2.10"), 962),
            ]
        )
    )
    assert bet.legs[1].leg.commission_bp == 500
    assert bet.legs[0].leg.commission_bp == 0


def test_cannot_lay_at_a_bookmaker(repo: Repository):
    with pytest.raises(IllegalActionError):
        repo.create_bet(
            f1_bet(
                legs=[
                    NewLeg(Side.BACK, "Bet365", D("2.00"), 1000),
                    NewLeg(Side.LAY, "Coral", D("2.10"), 962, commission_bp=200),
                ]
            )
        )


def test_create_validates_input(repo: Repository):
    with pytest.raises(ValidationError):
        repo.create_bet(f1_bet(event_name="  "))
    with pytest.raises(ValidationError):
        repo.create_bet(f1_bet(legs=[]))
    with pytest.raises(ValidationError):  # selection outside a 2-outcome market
        repo.create_bet(f1_bet(legs=[NewLeg(Side.BACK, "Bet365", D("2.00"), 1000, selection=2)]))
    with pytest.raises(StorageError):  # unknown offer id fails the foreign key
        repo.create_bet(f1_bet(offer_id="nope"))
    assert repo.list_bets() == []


def test_backfill_creates_a_settled_bet(repo: Repository, clock: FakeClock):
    placed = datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
    settled = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
    bet = repo.create_bet(
        f1_bet(
            placed_at=placed,
            settled_at=settled,
            legs=[
                NewLeg(Side.BACK, "Bet365", D("2.00"), 1000, result=LegResult.LOST),
                NewLeg(
                    Side.LAY, "Smarkets", D("2.10"), 962, commission_bp=200, result=LegResult.WON
                ),
            ],
        )
    )
    assert bet.status is BetStatus.SETTLED
    assert bet.expected_pl_pence == -58  # as it would have been at placement
    assert bet.actual_pl_pence == -57
    assert bet.placed_at == placed
    assert bet.settled_at == settled
    assert repo.audit_trail(bet.id)[0].new_value == "backfill"


# ------------------------------------------------------------------ settle


def test_settle_two_way(repo: Repository, clock: FakeClock):
    bet = repo.create_bet(f1_bet())
    clock.advance(hours=3)
    settled = repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    assert settled.status is BetStatus.SETTLED
    assert settled.actual_pl_pence == -58
    assert settled.effective_actual_pl == -58
    assert settled.settled_at == clock.now
    assert [r.leg.result for r in settled.legs] == [LegResult.WON, LegResult.LOST]
    trail = kinds(repo, bet.id)
    assert "SETTLED:legs[0].result" in trail
    assert "SETTLED:status" in trail
    with pytest.raises(IllegalActionError):
        repo.settle_two_way(bet.id, TwoWayOutcome.LAY_WON)


def test_settle_void_and_lay_won(repo: Repository):
    a = repo.create_bet(f1_bet())
    b = repo.create_bet(f1_bet())
    assert repo.settle_two_way(a.id, TwoWayOutcome.VOID).status is BetStatus.VOID
    assert repo.get_bet(a.id).actual_pl_pence == 0
    assert repo.settle_two_way(b.id, TwoWayOutcome.LAY_WON).actual_pl_pence == -57


def test_partial_settlement_with_cash_out(repo: Repository, clock: FakeClock):
    bet = repo.create_bet(f1_bet())
    partial = repo.settle(bet.id, {0: (LegResult.CASHED_OUT, 1000)})
    assert partial.status is BetStatus.PARTIALLY_SETTLED
    assert partial.actual_pl_pence is None
    assert partial.settled_at is None
    assert partial.live_pl_vector == (1000 - 1058, 1000 + 943)
    assert partial.live_guaranteed_pl == -58
    done = repo.settle(bet.id, {1: LegResult.WON})
    assert done.status is BetStatus.SETTLED
    assert done.actual_pl_pence == 1943  # F14a
    assert "SETTLED:legs[0].settled_amount" in kinds(repo, bet.id)


def test_settle_rejects_bad_requests(repo: Repository):
    bet = repo.create_bet(f1_bet())
    with pytest.raises(ValidationError):
        repo.settle(bet.id, {})
    with pytest.raises(NotFoundError):
        repo.settle(bet.id, {5: LegResult.WON})
    with pytest.raises(ValidationError):
        repo.settle(bet.id, {0: LegResult.PENDING})
    with pytest.raises(ValidationError):  # CASHED_OUT needs an amount
        repo.settle(bet.id, {0: LegResult.CASHED_OUT})
    assert repo.get_bet(bet.id).status is BetStatus.OPEN
    with pytest.raises(NotFoundError):
        repo.settle("missing", {0: LegResult.WON})


def test_settle_two_way_needs_two_legs(repo: Repository):
    dutch = repo.create_bet(
        NewBet(
            BetType.DUTCHING,
            "Three-way",
            market_outcomes=3,
            legs=[
                NewLeg(Side.BACK, "Bet365", D("2.50"), 1200, selection=0),
                NewLeg(Side.BACK, "Coral", D("3.40"), 882, selection=1),
                NewLeg(Side.BACK, "Betfred", D("3.10"), 968, selection=2),
            ],
        )
    )
    assert dutch.expected_pl_pence == -51
    with pytest.raises(IllegalActionError):
        repo.settle_two_way(dutch.id, TwoWayOutcome.BACK_WON)
    settled = repo.settle(dutch.id, {0: LegResult.LOST, 1: LegResult.WON, 2: LegResult.LOST})
    assert settled.actual_pl_pence == -51


# ------------------------------------------------------------------ reopen


def test_reopen_clears_realised_figures_and_override(repo: Repository):
    bet = repo.create_bet(f1_bet())
    repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    repo.adjust(bet.id, -20, "bookmaker refunded part of the stake")
    reopened = repo.reopen(bet.id)
    assert reopened.status is BetStatus.OPEN
    assert reopened.actual_pl_pence is None
    assert reopened.actual_pl_override_pence is None
    assert reopened.settled_at is None
    assert all(r.leg.result is LegResult.PENDING for r in reopened.legs)
    trail = kinds(repo, bet.id)
    assert "REOPENED:actual_pl_override_pence" in trail
    assert "REOPENED:status" in trail
    with pytest.raises(IllegalActionError):
        repo.reopen(bet.id)


def test_reopen_one_leg_of_a_partially_settled_bet(repo: Repository):
    bet = repo.create_bet(f1_bet())
    repo.settle(bet.id, {0: LegResult.WON})
    with pytest.raises(IllegalActionError):
        repo.reopen(bet.id, [1])
    reopened = repo.reopen(bet.id, [0])
    assert reopened.status is BetStatus.OPEN


# ------------------------------------------------------------------ adjust


def test_adjust_requires_settled_and_a_reason(repo: Repository):
    bet = repo.create_bet(f1_bet())
    with pytest.raises(IllegalActionError):
        repo.adjust(bet.id, 0, "too early")
    repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    with pytest.raises(ValidationError):
        repo.adjust(bet.id, 0, "   ")
    adjusted = repo.adjust(bet.id, 0, "2-up early payout eliminated the loss")
    assert adjusted.actual_pl_pence == -58
    assert adjusted.actual_pl_override_pence == 0
    assert adjusted.effective_actual_pl == 0
    assert adjusted.has_adjustments
    row = next(r for r in repo.audit_trail(bet.id) if r.kind is AuditKind.ADJUSTMENT)
    assert (row.old_value, row.new_value, row.reason) == (
        None,
        "0",
        "2-up early payout eliminated the loss",
    )
    cleared = repo.adjust(bet.id, None, "entered on the wrong bet")
    assert cleared.effective_actual_pl == -58
    assert cleared.has_adjustments  # the history remains


# ------------------------------------------------------------- edit/correct


def test_edit_open_bet_recomputes_expectation(repo: Repository):
    bet = repo.create_bet(f1_bet())
    edited = repo.edit_bet(
        bet.id,
        event_name="Arsenal v Chelsea (moved)",
        legs=[
            NewLeg(Side.BACK, "Bet365", D("2.10"), 1000),  # better price
            NewLeg(Side.LAY, "Smarkets", D("2.10"), 1010, commission_bp=200),
        ],
    )
    assert edited.event_name == "Arsenal v Chelsea (moved)"
    assert edited.legs[0].leg.odds == D("2.10")
    assert edited.expected_pl_pence == edited.live_guaranteed_pl
    assert edited.expected_pl_pence != -58
    trail = kinds(repo, bet.id)
    assert "EDITED:event_name" in trail
    assert "EDITED:legs[0].odds" in trail
    assert "EDITED:legs[1].stake" in trail
    assert "EDITED:expected_pl_pence" in trail
    assert not edited.has_adjustments  # edits are not adjustments


def test_edit_no_op_writes_nothing(repo: Repository):
    bet = repo.create_bet(f1_bet())
    same = repo.edit_bet(bet.id, event_name=bet.event_name, notes="")
    assert same == bet
    assert len(repo.audit_trail(bet.id)) == 1


def test_edit_rejects_settled_bets_unknown_fields_and_results(repo: Repository):
    bet = repo.create_bet(f1_bet())
    with pytest.raises(ValidationError):
        repo.edit_bet(bet.id, colour="red")
    with pytest.raises(IllegalActionError):
        repo.edit_bet(
            bet.id,
            legs=[
                NewLeg(Side.BACK, "Bet365", D("2.00"), 1000, result=LegResult.WON),
                NewLeg(Side.LAY, "Smarkets", D("2.10"), 962, commission_bp=200),
            ],
        )
    with pytest.raises(ValidationError):  # selection index 1 needs 2 outcomes, fine; 3 does not
        repo.edit_bet(bet.id, legs=[NewLeg(Side.BACK, "Bet365", D("2.00"), 1000, selection=3)])
    repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    with pytest.raises(IllegalActionError):
        repo.edit_bet(bet.id, notes="late")


def test_correct_settled_bet_keeps_expectation_and_results(repo: Repository):
    bet = repo.create_bet(f1_bet())
    repo.settle_two_way(bet.id, TwoWayOutcome.LAY_WON)
    with pytest.raises(ValidationError):
        repo.correct_bet(bet.id, reason="", notes="x")
    corrected = repo.correct_bet(
        bet.id,
        reason="lay stake was mistyped",
        legs=[
            NewLeg(Side.BACK, "Bet365", D("2.00"), 1000),
            NewLeg(Side.LAY, "Smarkets", D("2.10"), 1000, commission_bp=200),
        ],
    )
    assert corrected.expected_pl_pence == -58  # frozen
    assert corrected.status is BetStatus.SETTLED
    assert [r.leg.result for r in corrected.legs] == [LegResult.LOST, LegResult.WON]
    assert corrected.actual_pl_pence == -20  # F7 lay-wins branch
    assert corrected.has_adjustments
    rows = [r for r in repo.audit_trail(bet.id) if r.kind is AuditKind.CORRECTION]
    assert {r.field for r in rows} == {"legs[1].stake"}
    assert rows[0].reason == "lay stake was mistyped"
    with pytest.raises(IllegalActionError):
        repo.correct_bet(
            bet.id, reason="drop a leg", legs=[NewLeg(Side.BACK, "Bet365", D("2.0"), 1)]
        )
    fresh = repo.create_bet(f1_bet())
    with pytest.raises(IllegalActionError):
        repo.correct_bet(fresh.id, reason="wrong path", notes="x")


# ------------------------------------------------------- delete/restore/flags


def test_soft_delete_and_restore(repo: Repository):
    bet = repo.create_bet(f1_bet())
    deleted = repo.soft_delete(bet.id)
    assert deleted.is_deleted
    assert repo.list_bets() == []
    assert [b.id for b in repo.list_bets(BetFilter(include_deleted=True))] == [bet.id]
    assert repo.get_bet(bet.id).is_deleted
    with pytest.raises(IllegalActionError):
        repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    with pytest.raises(IllegalActionError):
        repo.edit_bet(bet.id, notes="x")
    restored = repo.restore(bet.id)
    assert not restored.is_deleted
    assert kinds(repo, bet.id)[-2:] == ["DELETED:deleted_at", "RESTORED:deleted_at"]


def test_needs_review_flag(repo: Repository):
    bet = repo.create_bet(f1_bet())
    flagged = repo.set_needs_review(bet.id, True, "check the boosted price")
    assert flagged.needs_review
    assert repo.list_bets(BetFilter(needs_review=True))[0].id == bet.id
    assert repo.list_bets(BetFilter(needs_review=False)) == []
    assert repo.set_needs_review(bet.id, True) == flagged  # no-op


# ------------------------------------------------------------------ filters


def test_list_bets_filters(repo: Repository, clock: FakeClock):
    early = repo.create_bet(f1_bet(notes="50% back_stake offer"))
    clock.advance(days=1)
    later = repo.create_bet(snr_bet())
    clock.advance(days=1)
    settled = repo.create_bet(f1_bet(event_name="Leeds v Villa"))
    repo.settle_two_way(settled.id, TwoWayOutcome.BACK_WON)

    assert [b.id for b in repo.list_bets()] == [settled.id, later.id, early.id]
    assert [b.id for b in repo.list_bets(BetFilter(statuses=frozenset({BetStatus.OPEN})))] == [
        later.id,
        early.id,
    ]
    assert [
        b.id for b in repo.list_bets(BetFilter(bet_types=frozenset({BetType.FREE_BET_SNR})))
    ] == [later.id]
    assert [b.id for b in repo.list_bets(BetFilter(bookmaker="william hill"))] == [later.id]
    assert len(repo.list_bets(BetFilter(exchange="Smarkets"))) == 3
    assert [b.id for b in repo.list_bets(BetFilter(text="leeds"))] == [settled.id]
    assert [b.id for b in repo.list_bets(BetFilter(text="50% back_stake"))] == [early.id]
    assert repo.list_bets(BetFilter(text="50%_back")) == []  # _ and % are literal
    assert [b.id for b in repo.list_bets(BetFilter(placed_from=later.placed_at))] == [
        settled.id,
        later.id,
    ]
    assert [b.id for b in repo.list_bets(BetFilter(placed_to=later.placed_at))] == [early.id]
    assert repo.list_bets(BetFilter(statuses=frozenset())) == []


def test_offers_and_parent_links(repo: Repository):
    bet365 = repo.find_venue("Bet365")
    assert bet365 is not None
    offer = repo.create_offer("Bet £10 get £10", venue_id=bet365.id)
    qualifier = repo.create_bet(f1_bet(offer_id=offer.id))
    free = repo.create_bet(snr_bet(offer_id=offer.id, parent_bet_id=qualifier.id))
    assert free.parent_bet_id == qualifier.id
    assert {b.id for b in repo.list_bets(BetFilter(offer_id=offer.id))} == {qualifier.id, free.id}
    assert repo.list_offers()[0].title == "Bet £10 get £10"
    with pytest.raises(ValidationError):
        repo.create_offer("  ")


def test_venue_management(repo: Repository):
    smarkets = repo.find_venue("Smarkets")
    assert smarkets is not None
    updated = repo.update_venue(smarkets.id, default_commission_bp=100, min_stake_pence=10)
    assert (updated.default_commission_bp, updated.min_stake_pence) == (100, 10)
    assert repo.ensure_venue("smarkets", VenueKind.EXCHANGE) == updated
    with pytest.raises(IllegalActionError):
        repo.ensure_venue("Smarkets", VenueKind.BOOKMAKER)
    with pytest.raises(ValidationError):
        repo.ensure_venue("  ", VenueKind.BOOKMAKER)
    exchanges = repo.list_venues(VenueKind.EXCHANGE)
    assert all(v.kind is VenueKind.EXCHANGE for v in exchanges)
    with pytest.raises(NotFoundError):
        repo.get_venue("nope")


def test_audit_trail_is_chronological(repo: Repository, clock: FakeClock):
    bet = repo.create_bet(f1_bet())
    clock.advance(minutes=1)
    repo.set_needs_review(bet.id, True)
    clock.advance(minutes=1)
    repo.settle_two_way(bet.id, TwoWayOutcome.BACK_WON)
    trail = repo.audit_trail(bet.id)
    assert [r.kind for r in trail][:2] == [AuditKind.CREATED, AuditKind.EDITED]
    assert all(a.at <= b.at for a, b in pairwise(trail))
    with pytest.raises(NotFoundError):
        repo.audit_trail("missing")
