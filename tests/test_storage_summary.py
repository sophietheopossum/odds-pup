from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from odds_pup.core import LegResult, Side, TwoWayOutcome
from odds_pup.storage import NewLeg, Repository
from tests.storage_helpers import FakeClock, f1_bet, snr_bet

D = Decimal


def test_summary_buckets_by_local_month_and_excludes_deleted(repo: Repository, clock: FakeClock):
    # settled at 23:30Z on 31 Aug = 00:30 BST on 1 Sep -> September
    clock.now = datetime(2026, 8, 31, 23, 30, tzinfo=UTC)
    september = repo.create_bet(f1_bet())
    repo.settle_two_way(september.id, TwoWayOutcome.BACK_WON)  # -58
    # settled at 22:30Z on 31 Aug = 23:30 BST on 31 Aug -> August
    clock.now = datetime(2026, 8, 31, 22, 30, tzinfo=UTC)
    august = repo.create_bet(snr_bet())
    repo.settle_two_way(august.id, TwoWayOutcome.LAY_WON)  # +471
    clock.now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    deleted = repo.create_bet(f1_bet())
    repo.settle_two_way(deleted.id, TwoWayOutcome.BACK_WON)
    repo.soft_delete(deleted.id)
    open_bet = repo.create_bet(f1_bet(event_name="Open one"))

    summary = repo.summary(now=datetime(2026, 9, 15, 12, 0, tzinfo=UTC))
    assert summary.realised_all_time == -58 + 471
    assert summary.realised_this_month == -58
    assert summary.settled_count == 2
    assert summary.open_count == 1
    assert summary.open_guaranteed == -58
    assert summary.open_liability_by_exchange == {"Smarkets": 1058}
    assert summary.realised_by_bookmaker == {"Bet365": -58, "William Hill": 471}
    assert open_bet.status.value == "OPEN"


def test_summary_uses_overrides_and_live_partial_positions(repo: Repository):
    settled = repo.create_bet(f1_bet())
    repo.settle_two_way(settled.id, TwoWayOutcome.BACK_WON)
    repo.adjust(settled.id, 0, "2-up paid out")
    partial = repo.create_bet(f1_bet(event_name="Partial"))
    repo.settle(partial.id, {0: (LegResult.CASHED_OUT, 1000)})
    unlaid = repo.create_bet(
        f1_bet(
            event_name="Unlaid",
            legs=[
                NewLeg(Side.BACK, "Bet365", D("2.00"), 1000),
                NewLeg(Side.LAY, "Betfair Exchange", D("2.10"), 0, commission_bp=200),
            ],
        )
    )
    summary = repo.summary()
    assert summary.realised_all_time == 0  # override wins
    assert summary.realised_by_bookmaker == {"Bet365": 0}
    # partial: min(1000-1058, 1000+943) = -58 ; unlaid: min(1000, -1000) = -1000
    assert summary.open_guaranteed == -58 + -1000
    assert summary.open_liability_by_exchange == {"Smarkets": 1058}  # zero-stake lay adds nothing
    assert summary.open_count == 2
    assert unlaid.live_pl_vector == (1000, -1000)


def test_empty_ledger_summary(repo: Repository):
    summary = repo.summary()
    assert summary.realised_all_time == 0
    assert summary.realised_this_month == 0
    assert summary.open_guaranteed == 0
    assert summary.open_liability_by_exchange == {}
    assert summary.realised_by_bookmaker == {}
