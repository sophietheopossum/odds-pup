"""Domain enumerations and the Leg value object. SPEC §3."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import localcontext
from enum import StrEnum
from typing import TYPE_CHECKING

from odds_pup.core.errors import CoreError
from odds_pup.core.money import DECIMAL_CONTEXT, MAX_MONEY_PENCE, Pence, to_pence
from odds_pup.core.odds import validate_odds

if TYPE_CHECKING:
    from decimal import Decimal

MAX_COMMISSION_BP = 9999
"""Commission is stored in basis points and must be strictly below 100%."""


class BetType(StrEnum):
    QUALIFYING = "QUALIFYING"
    FREE_BET_SNR = "FREE_BET_SNR"
    FREE_BET_SR = "FREE_BET_SR"
    DUTCHING = "DUTCHING"


class BetStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_SETTLED = "PARTIALLY_SETTLED"
    SETTLED = "SETTLED"
    VOID = "VOID"


class Side(StrEnum):
    BACK = "BACK"
    LAY = "LAY"


class StakeKind(StrEnum):
    CASH = "CASH"
    FREE_SNR = "FREE_SNR"
    FREE_SR = "FREE_SR"


class LegResult(StrEnum):
    PENDING = "PENDING"
    WON = "WON"
    LOST = "LOST"
    VOID = "VOID"
    CASHED_OUT = "CASHED_OUT"


class VenueKind(StrEnum):
    BOOKMAKER = "BOOKMAKER"
    EXCHANGE = "EXCHANGE"


class AuditKind(StrEnum):
    CREATED = "CREATED"
    EDITED = "EDITED"
    SETTLED = "SETTLED"
    REOPENED = "REOPENED"
    ADJUSTMENT = "ADJUSTMENT"
    CORRECTION = "CORRECTION"
    DELETED = "DELETED"
    RESTORED = "RESTORED"


class ValidationError(CoreError):
    """A domain value violates SPEC §14's hard rules."""


def validate_commission_bp(commission_bp: int) -> int:
    if isinstance(commission_bp, bool) or not isinstance(commission_bp, int):
        raise ValidationError(f"commission must be an int in basis points: {commission_bp!r}")
    if not 0 <= commission_bp <= MAX_COMMISSION_BP:
        raise ValidationError(f"commission must be 0..{MAX_COMMISSION_BP} bp: {commission_bp}")
    return commission_bp


def validate_pence(
    value: object,
    *,
    name: str,
    minimum: int = 0,
    maximum: int = MAX_MONEY_PENCE,
) -> Pence:
    """Return ``value`` if it is a plain int within ``[minimum, maximum]`` pence."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an int number of pence: {value!r}")
    if value < minimum:
        raise ValidationError(f"{name} must be >= {minimum} pence: {value}")
    if value > maximum:
        raise ValidationError(f"{name} must be <= {maximum} pence: {value}")
    return value


@dataclass(frozen=True, slots=True)
class Leg:
    """One bet at one venue on one selection.

    ``stake`` is the back stake for a BACK leg and the lay stake (the backer's stake the layer
    accepts) for a LAY leg. ``venue`` is a display name or the storage layer's venue id; legs
    whose :attr:`venue_key` (trimmed, case-folded) matches form one commission group (SPEC §5.3).
    """

    side: Side
    venue: str
    odds: Decimal
    stake: Pence
    selection: int = 0
    stake_kind: StakeKind = StakeKind.CASH
    commission_bp: int = 0
    result: LegResult = LegResult.PENDING
    settled_amount: Pence | None = None
    odds_text: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.venue, str) or not self.venue.strip():
            raise ValidationError("leg venue must not be empty")
        object.__setattr__(self, "venue", self.venue.strip())
        validate_odds(self.odds)
        validate_pence(self.stake, name="stake")
        if isinstance(self.selection, bool) or not isinstance(self.selection, int):
            raise ValidationError(f"selection index must be an int: {self.selection!r}")
        if self.selection < 0:
            raise ValidationError(f"selection index must be >= 0: {self.selection}")
        validate_commission_bp(self.commission_bp)
        if self.side is Side.LAY and self.stake_kind is not StakeKind.CASH:
            raise ValidationError("a LAY leg is always CASH; free bets cannot be laid")
        has_amount = self.settled_amount is not None
        if (self.result is LegResult.CASHED_OUT) != has_amount:
            raise ValidationError(
                "settled_amount is required for CASHED_OUT and forbidden otherwise"
            )
        if has_amount:
            validate_pence(self.settled_amount, name="settled_amount", minimum=-MAX_MONEY_PENCE)

    @property
    def venue_key(self) -> str:
        """The key commission netting groups by: trimmed and case-folded venue."""
        return self.venue.casefold()

    @property
    def is_settled(self) -> bool:
        return self.result is not LegResult.PENDING

    @property
    def is_free(self) -> bool:
        return self.stake_kind is not StakeKind.CASH

    @property
    def liability(self) -> Pence:
        """Cash the venue holds against this leg.

        ``L * (O - 1)`` for a lay; the stake for a cash back; nothing for a free bet.
        """
        if self.side is Side.LAY:
            with localcontext(DECIMAL_CONTEXT):
                return to_pence(self.stake * (self.odds - 1))
        return 0 if self.is_free else self.stake

    def settled(self, result: LegResult, settled_amount: Pence | None = None) -> Leg:
        """A copy of this leg with a result applied (validation runs again)."""
        return replace(self, result=result, settled_amount=settled_amount)

    def reopened(self) -> Leg:
        return replace(self, result=LegResult.PENDING, settled_amount=None)

    def cashed_out(self, net_pence: Pence) -> Leg:
        """Settle as CASHED_OUT with a net P/L figure (what an exchange cash-out displays)."""
        return self.settled(LegResult.CASHED_OUT, net_pence)

    def cashed_out_from_return(self, return_pence: Pence) -> Leg:
        """Settle a BACK leg as CASHED_OUT given the *return* the bookmaker credited (SPEC §8.4).

        A cash stake comes off the return (a 2-up early payout of £20.00 on a £10.00 bet nets
        £10.00); a free bet's return is all profit. Lay legs do not have a "return": exchanges
        show a net figure, so use :meth:`cashed_out` for those.
        """
        if self.side is not Side.BACK:
            raise ValidationError(
                "cashed_out_from_return applies to BACK legs; lays are net already"
            )
        validate_pence(return_pence, name="return")
        net = return_pence if self.is_free else return_pence - self.stake
        return self.settled(LegResult.CASHED_OUT, net)
