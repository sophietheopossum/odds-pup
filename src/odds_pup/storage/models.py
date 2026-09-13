"""Records the repository reads and writes. SPEC §3, §10."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from odds_pup.core import (
    AuditKind,
    BetStatus,
    BetType,
    Leg,
    LegResult,
    Pence,
    Side,
    StakeKind,
    VenueKind,
    guaranteed_pl,
    open_liability,
    pl_vector,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Venue:
    id: str
    name: str
    kind: VenueKind
    default_commission_bp: int
    min_stake_pence: Pence | None
    ladder: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Offer:
    id: str
    venue_id: str | None
    title: str
    notes: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NewLeg:
    """Input for one leg. ``venue`` is a venue name; unknown names create a venue."""

    side: Side
    venue: str
    odds: Decimal
    stake: Pence
    selection: int = 0
    stake_kind: StakeKind = StakeKind.CASH
    commission_bp: int | None = None
    """``None`` means the venue's default commission (0 for bookmakers)."""
    result: LegResult = LegResult.PENDING
    settled_amount: Pence | None = None
    odds_text: str | None = None

    @classmethod
    def from_leg(cls, leg: Leg, *, keep_result: bool = False) -> NewLeg:
        """Input for ``leg`` with its commission snapshot kept explicit (not the venue default).

        Results are dropped unless ``keep_result`` (backfill from a settled core leg).
        """
        return cls(
            side=leg.side,
            venue=leg.venue,
            odds=leg.odds,
            stake=leg.stake,
            selection=leg.selection,
            stake_kind=leg.stake_kind,
            commission_bp=leg.commission_bp,
            result=leg.result if keep_result else LegResult.PENDING,
            settled_amount=leg.settled_amount if keep_result else None,
            odds_text=leg.odds_text,
        )


@dataclass(frozen=True, slots=True)
class NewBet:
    """Input for :meth:`Repository.create_bet`. Results on legs mean a backfilled bet."""

    bet_type: BetType
    event_name: str
    legs: Sequence[NewLeg]
    selection: str = ""
    market: str = ""
    market_outcomes: int = 2
    offer_id: str | None = None
    parent_bet_id: str | None = None
    placed_at: datetime | None = None
    event_at: datetime | None = None
    settled_at: datetime | None = None
    notes: str = ""
    needs_review: bool = False

    @classmethod
    def from_record(cls, bet: BetRecord) -> NewBet:
        """A fresh, unsettled copy of ``bet`` for Clone (SPEC §13): new id and placement time."""
        return cls(
            bet_type=bet.bet_type,
            event_name=bet.event_name,
            legs=[NewLeg.from_leg(record.leg) for record in bet.legs],
            selection=bet.selection,
            market=bet.market,
            market_outcomes=bet.market_outcomes,
            offer_id=bet.offer_id,
            parent_bet_id=bet.parent_bet_id,
            event_at=bet.event_at,
            notes=bet.notes,
        )


@dataclass(frozen=True, slots=True)
class LegRecord:
    id: str
    venue_id: str
    leg: Leg
    """The domain leg; ``leg.venue`` is the venue's display name."""


@dataclass(frozen=True, slots=True)
class BetRecord:
    id: str
    bet_type: BetType
    status: BetStatus
    event_name: str
    selection: str
    market: str
    market_outcomes: int
    offer_id: str | None
    parent_bet_id: str | None
    currency: str
    placed_at: datetime
    event_at: datetime | None
    settled_at: datetime | None
    expected_pl_pence: Pence
    actual_pl_pence: Pence | None
    actual_pl_override_pence: Pence | None
    needs_review: bool
    notes: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    legs: tuple[LegRecord, ...]
    has_adjustments: bool = False

    @property
    def core_legs(self) -> tuple[Leg, ...]:
        return tuple(record.leg for record in self.legs)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def is_settled(self) -> bool:
        return self.status in (BetStatus.SETTLED, BetStatus.VOID)

    @property
    def effective_actual_pl(self) -> Pence | None:
        """Override if set, else the derived figure (SPEC §8.2)."""
        if self.actual_pl_override_pence is not None:
            return self.actual_pl_override_pence
        return self.actual_pl_pence

    @property
    def live_pl_vector(self) -> tuple[Pence, ...]:
        return pl_vector(self.core_legs, self.market_outcomes)

    @property
    def live_guaranteed_pl(self) -> Pence:
        """Current worst case with settled legs as constants (SPEC §5.4)."""
        return guaranteed_pl(self.core_legs, self.market_outcomes)

    @property
    def open_liability(self) -> Pence:
        return open_liability(self.core_legs)

    @property
    def back_legs(self) -> tuple[LegRecord, ...]:
        return tuple(r for r in self.legs if r.leg.side is Side.BACK)

    @property
    def lay_legs(self) -> tuple[LegRecord, ...]:
        return tuple(r for r in self.legs if r.leg.side is Side.LAY)

    @property
    def bookmaker(self) -> str | None:
        """Venue of the first back leg (what the ledger's bookmaker column shows)."""
        backs = self.back_legs
        return backs[0].leg.venue if backs else None

    @property
    def exchange(self) -> str | None:
        lays = self.lay_legs
        return lays[0].leg.venue if lays else None


@dataclass(frozen=True, slots=True)
class AuditRow:
    id: str
    bet_id: str
    action_id: str
    at: datetime
    kind: AuditKind
    field: str | None
    old_value: str | None
    new_value: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class BetFilter:
    """Ledger filters (SPEC §13). ``None`` means no restriction."""

    statuses: frozenset[BetStatus] | None = None
    bet_types: frozenset[BetType] | None = None
    bookmaker: str | None = None
    exchange: str | None = None
    placed_from: datetime | None = None
    placed_to: datetime | None = None
    """Exclusive upper bound."""
    text: str | None = None
    offer_id: str | None = None
    needs_review: bool | None = None
    include_deleted: bool = False
    limit: int | None = None
    """Newest ``limit`` bets only (after the other filters)."""


@dataclass(frozen=True, slots=True)
class Summary:
    """The summary strip (SPEC §5.4, §13). Soft-deleted bets are excluded from everything."""

    realised_all_time: Pence
    realised_this_month: Pence
    open_guaranteed: Pence
    open_liability_by_exchange: dict[str, Pence] = field(default_factory=dict)
    realised_by_bookmaker: dict[str, Pence] = field(default_factory=dict)
    open_count: int = 0
    settled_count: int = 0


class AuditValueKind(StrEnum):
    """How to read an audit row's old/new text (SPEC §10 stores the stored text form)."""

    NONE = "NONE"
    TEXT = "TEXT"
    MONEY = "MONEY"
    ODDS = "ODDS"
    COMMISSION = "COMMISSION"
    TIMESTAMP = "TIMESTAMP"
    FLAG = "FLAG"
    COUNT = "COUNT"
    ENUM = "ENUM"


_LEG_FIELD = re.compile(r"^legs\[(\d+)\](?:\.(\w+))?$", re.ASCII)
_HEADER_KINDS: dict[str, AuditValueKind] = {
    "bet_type": AuditValueKind.ENUM,
    "event_name": AuditValueKind.TEXT,
    "selection": AuditValueKind.TEXT,
    "market": AuditValueKind.TEXT,
    "notes": AuditValueKind.TEXT,
    "offer_id": AuditValueKind.TEXT,
    "parent_bet_id": AuditValueKind.TEXT,
    "market_outcomes": AuditValueKind.COUNT,
    "placed_at": AuditValueKind.TIMESTAMP,
    "event_at": AuditValueKind.TIMESTAMP,
    "settled_at": AuditValueKind.TIMESTAMP,
    "deleted_at": AuditValueKind.TIMESTAMP,
    "expected_pl_pence": AuditValueKind.MONEY,
    "actual_pl_override_pence": AuditValueKind.MONEY,
    "needs_review": AuditValueKind.FLAG,
    "status": AuditValueKind.ENUM,
}
_LEG_KINDS: dict[str, AuditValueKind] = {
    "side": AuditValueKind.ENUM,
    "venue": AuditValueKind.TEXT,
    "odds": AuditValueKind.ODDS,
    "stake": AuditValueKind.MONEY,
    "selection": AuditValueKind.COUNT,
    "stake_kind": AuditValueKind.ENUM,
    "commission_bp": AuditValueKind.COMMISSION,
    "odds_text": AuditValueKind.TEXT,
    "result": AuditValueKind.ENUM,
    "settled_amount": AuditValueKind.MONEY,
}


def audit_value_kind(field: str | None) -> AuditValueKind:
    """The kind of value an audit row for ``field`` carries, for display."""
    if field is None:
        return AuditValueKind.NONE
    match = _LEG_FIELD.match(field)
    if match is not None:
        return _LEG_KINDS.get(match[2] or "", AuditValueKind.TEXT)
    return _HEADER_KINDS.get(field, AuditValueKind.TEXT)
