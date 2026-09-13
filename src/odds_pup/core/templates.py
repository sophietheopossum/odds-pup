"""Bet-type templates: optimal lay stakes, ratings, two-way positions, dutching. SPEC §6."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import StrEnum
from fractions import Fraction
from typing import TYPE_CHECKING

from odds_pup.core.commission import commission_fraction
from odds_pup.core.model import (
    BetType,
    Leg,
    LegResult,
    Side,
    StakeKind,
    ValidationError,
    validate_pence,
)
from odds_pup.core.money import DECIMAL_CONTEXT, Pence, to_pence
from odds_pup.core.odds import validate_odds
from odds_pup.core.pl import pl_vector

if TYPE_CHECKING:
    from collections.abc import Sequence

_BACK_KIND: dict[BetType, StakeKind] = {
    BetType.QUALIFYING: StakeKind.CASH,
    BetType.FREE_BET_SNR: StakeKind.FREE_SNR,
    BetType.FREE_BET_SR: StakeKind.FREE_SR,
}
TWO_WAY_TYPES = frozenset(_BACK_KIND)
_PERCENT_QUANTUM = Decimal("0.01")


def _back_return_multiplier(bet_type: BetType, back_odds: Decimal) -> Decimal:
    """``B - 1`` for SNR (winnings only), ``B`` for cash and SR (stake comes back too)."""
    if bet_type not in TWO_WAY_TYPES:
        raise ValidationError(f"{bet_type} is not a one-back-one-lay template")
    return back_odds - 1 if bet_type is BetType.FREE_BET_SNR else back_odds


def optimal_lay_stake(
    bet_type: BetType,
    back_stake: Pence,
    back_odds: Decimal,
    lay_odds: Decimal,
    commission_bp: int,
) -> Pence:
    """The lay stake that equalises both outcomes (§6.1), rounded to pence (§4 point 1).

    QUALIFYING and FREE_BET_SR: ``S*B / (O - c)``. FREE_BET_SNR: ``S*(B - 1) / (O - c)``.
    """
    validate_pence(back_stake, name="back_stake")
    validate_odds(back_odds)
    validate_odds(lay_odds)
    with localcontext(DECIMAL_CONTEXT):
        numerator = back_stake * _back_return_multiplier(bet_type, back_odds)
        return to_pence(numerator / (lay_odds - commission_fraction(commission_bp)))


def exact_rating(
    bet_type: BetType, back_odds: Decimal, lay_odds: Decimal, commission_bp: int
) -> Decimal:
    """Unrounded rating (§6.3).

    ``100*B(1-c)/(O-c)`` for QL and SR, ``100*(B-1)(1-c)/(O-c)`` for SNR.
    """
    validate_odds(back_odds)
    validate_odds(lay_odds)
    with localcontext(DECIMAL_CONTEXT):
        c = commission_fraction(commission_bp)
        return 100 * _back_return_multiplier(bet_type, back_odds) * (1 - c) / (lay_odds - c)


def rating(bet_type: BetType, back_odds: Decimal, lay_odds: Decimal, commission_bp: int) -> Decimal:
    """Rating quantized to 2 dp HALF_UP for display (§4 point 5)."""
    with localcontext(DECIMAL_CONTEXT):
        return exact_rating(bet_type, back_odds, lay_odds, commission_bp).quantize(
            _PERCENT_QUANTUM, rounding=ROUND_HALF_UP
        )


class TwoWayOutcome(StrEnum):
    """The Settle dialog's buttons for a one-back-one-lay bet (§8.3)."""

    BACK_WON = "BACK_WON"
    LAY_WON = "LAY_WON"
    VOID = "VOID"


def settle_two_way(back: Leg, lay: Leg, outcome: TwoWayOutcome) -> tuple[Leg, Leg]:
    """Apply one of the ordinary two-way outcomes to a pair of pending legs (§8.3).

    Anything else (one-sided void, cash-out, early payout) goes through the per-leg editor,
    which uses :meth:`Leg.settled`, :meth:`Leg.cashed_out` and :meth:`Leg.cashed_out_from_return`
    directly. Refuses to overwrite a leg that already has a result; reopen it first.
    """
    if back.side is not Side.BACK or lay.side is not Side.LAY:
        raise ValidationError("settle_two_way needs a BACK leg and a LAY leg, in that order")
    if back.is_settled or lay.is_settled:
        raise ValidationError("a leg already has a result; reopen it before settling again")
    match outcome:
        case TwoWayOutcome.BACK_WON:
            return back.settled(LegResult.WON), lay.settled(LegResult.LOST)
        case TwoWayOutcome.LAY_WON:
            return back.settled(LegResult.LOST), lay.settled(LegResult.WON)
        case TwoWayOutcome.VOID:
            return back.settled(LegResult.VOID), lay.settled(LegResult.VOID)


@dataclass(frozen=True, slots=True)
class TwoWayPosition:
    """A one-back-one-lay position with everything the New Bet panel displays (§13)."""

    bet_type: BetType
    back: Leg
    lay: Leg
    suggested_lay_stake: Pence
    liability: Pence
    pl_if_back_wins: Pence
    pl_if_lay_wins: Pence
    guaranteed: Pence
    rating: Decimal

    @property
    def legs(self) -> tuple[Leg, Leg]:
        return (self.back, self.lay)

    @property
    def lay_stake_is_suggested(self) -> bool:
        return self.lay.stake == self.suggested_lay_stake

    def settled(self, outcome: TwoWayOutcome) -> tuple[Leg, Leg]:
        """Convenience for backfill: the legs with ``outcome`` applied."""
        return settle_two_way(self.back, self.lay, outcome)


def two_way_position(
    bet_type: BetType,
    back_stake: Pence,
    back_odds: Decimal,
    lay_odds: Decimal,
    commission_bp: int,
    *,
    lay_stake: Pence | None = None,
    bookmaker: str = "bookmaker",
    exchange: str = "exchange",
    back_odds_text: str | None = None,
    lay_odds_text: str | None = None,
) -> TwoWayPosition:
    """Build the legs for a template and evaluate them.

    ``lay_stake`` defaults to the optimal stake; pass the amount actually matched to see the
    real (unequal) branches. The bookmaker and exchange must be different venues (compared by
    :attr:`Leg.venue_key`), otherwise commission netting would merge the two legs.
    """
    if bet_type not in TWO_WAY_TYPES:
        raise ValidationError(f"{bet_type} is not a one-back-one-lay template")
    validate_pence(back_stake, name="back_stake", minimum=1)
    suggested = optimal_lay_stake(bet_type, back_stake, back_odds, lay_odds, commission_bp)
    actual_lay = suggested if lay_stake is None else validate_pence(lay_stake, name="lay_stake")
    back = Leg(
        side=Side.BACK,
        venue=bookmaker,
        odds=back_odds,
        stake=back_stake,
        stake_kind=_BACK_KIND[bet_type],
        odds_text=back_odds_text,
    )
    lay = Leg(
        side=Side.LAY,
        venue=exchange,
        odds=lay_odds,
        stake=actual_lay,
        commission_bp=commission_bp,
        odds_text=lay_odds_text,
    )
    if back.venue_key == lay.venue_key:
        raise ValidationError("back and lay must be at different venues")
    if_back_wins, if_lay_wins = pl_vector((back, lay), 2)
    return TwoWayPosition(
        bet_type=bet_type,
        back=back,
        lay=lay,
        suggested_lay_stake=suggested,
        liability=lay.liability,
        pl_if_back_wins=if_back_wins,
        pl_if_lay_wins=if_lay_wins,
        guaranteed=min(if_back_wins, if_lay_wins),
        rating=rating(bet_type, back_odds, lay_odds, commission_bp),
    )


def dutch_stakes(odds: Sequence[Decimal], target_return: Pence) -> tuple[Pence, ...]:
    """Cash stakes ``HALF_UP(R / B_i)``: near-equal returns whichever selection wins (§6.4)."""
    if len(odds) < 2:
        raise ValidationError("dutching needs at least two selections")
    validate_pence(target_return, name="target_return", minimum=1)
    with localcontext(DECIMAL_CONTEXT):
        return tuple(to_pence(Decimal(target_return) / validate_odds(b)) for b in odds)


def dutch_stakes_from_free_leg(
    odds: Sequence[Decimal], free_index: int, free_stake: Pence
) -> tuple[Pence, ...]:
    """Cash stakes covering the other selections against an SNR free bet on ``free_index`` (§6.4).

    The free leg returns ``S_k(B_k - 1)``, so each other leg stakes ``HALF_UP(S_k(B_k - 1)/B_i)``.
    """
    if len(odds) < 2:
        raise ValidationError("dutching needs at least two selections")
    if not 0 <= free_index < len(odds):
        raise ValidationError(f"free_index {free_index} is outside {len(odds)} selections")
    validate_pence(free_stake, name="free_stake", minimum=1)
    for b in odds:
        validate_odds(b)
    with localcontext(DECIMAL_CONTEXT):
        target = free_stake * (odds[free_index] - 1)
        return tuple(
            free_stake if i == free_index else to_pence(target / b) for i, b in enumerate(odds)
        )


def book_percentage(odds: Sequence[Decimal]) -> Decimal:
    """``100 * sum(1 / B_i)`` rounded once, HALF_UP, to 2 dp for display (§4 point 5).

    Computed with exact rationals so an exact tie such as ``96.875`` rounds up and the result
    does not depend on the order of the prices.
    """
    if not odds:
        raise ValidationError("book percentage needs at least one price")
    total = sum((Fraction(1) / Fraction(validate_odds(b)) for b in odds), Fraction(0))
    hundredths = total * 10_000  # 100 (percent) * 100 (two decimals)
    rounded = (2 * hundredths.numerator + hundredths.denominator) // (2 * hundredths.denominator)
    with localcontext(DECIMAL_CONTEXT):
        return Decimal(rounded).scaleb(-2)
