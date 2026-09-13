"""Shared helpers for storage tests: a controllable clock and canonical bets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from odds_pup.core import BetType, Side, StakeKind
from odds_pup.storage import NewBet, NewLeg

D = Decimal


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 12, 10, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: int) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now


def f1_bet(**overrides: object) -> NewBet:
    """SPEC F1: QL £10 @ 2.00 at Bet365, lay 9.62 @ 2.10 at Smarkets, 2%."""
    fields: dict[str, object] = {
        "bet_type": BetType.QUALIFYING,
        "event_name": "Arsenal v Chelsea",
        "selection": "Arsenal",
        "market": "Match Odds",
        "legs": [
            NewLeg(Side.BACK, "Bet365", D("2.00"), 1000),
            NewLeg(Side.LAY, "Smarkets", D("2.10"), 962, commission_bp=200),
        ],
    }
    fields.update(overrides)
    return NewBet(**fields)  # type: ignore[arg-type]


def snr_bet(**overrides: object) -> NewBet:
    """SPEC F2: SNR £10 @ 2.00, lay 4.81 @ 2.10, 2%."""
    fields: dict[str, object] = {
        "bet_type": BetType.FREE_BET_SNR,
        "event_name": "Spurs v Everton",
        "selection": "Spurs",
        "legs": [
            NewLeg(Side.BACK, "William Hill", D("2.00"), 1000, stake_kind=StakeKind.FREE_SNR),
            NewLeg(Side.LAY, "Smarkets", D("2.10"), 481, commission_bp=200),
        ],
    }
    fields.update(overrides)
    return NewBet(**fields)  # type: ignore[arg-type]
