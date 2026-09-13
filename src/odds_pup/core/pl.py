"""Profit/loss of a position as a vector over market outcomes. SPEC §5 and §8.

Every function here is pure and works on a sequence of :class:`Leg` values. Settled legs
contribute a constant to every outcome; pending legs vary by outcome; commission is netted per
venue per outcome and only charged on positive net winnings.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING

from odds_pup.core.model import BetStatus, Leg, LegResult, Side, StakeKind, ValidationError
from odds_pup.core.money import DECIMAL_CONTEXT, Pence, to_pence

if TYPE_CHECKING:
    from collections.abc import Sequence

MIN_MARKET_OUTCOMES = 2


def gross_if(leg: Leg, *, selection_wins: bool) -> Pence:
    """The §5.1 leg table: gross P/L in pence when the leg's selection wins or loses."""
    with localcontext(DECIMAL_CONTEXT):
        if leg.side is Side.BACK:
            if selection_wins:
                winnings_only = leg.stake_kind in (StakeKind.CASH, StakeKind.FREE_SNR)
                exact = leg.stake * (leg.odds - 1) if winnings_only else leg.stake * leg.odds
            else:
                exact = Decimal(-leg.stake) if leg.stake_kind is StakeKind.CASH else Decimal(0)
        elif selection_wins:
            exact = -leg.stake * (leg.odds - 1)
        else:
            exact = Decimal(leg.stake)
        return to_pence(exact)


def leg_gross(leg: Leg, outcome: int) -> Pence:
    """Gross contribution of ``leg`` to ``outcome`` (§5.1 for PENDING, §8.1 otherwise).

    ``CASHED_OUT`` legs return their net settled amount; callers must keep it out of the
    commission base (see :func:`outcome_pl`).
    """
    match leg.result:
        case LegResult.PENDING:
            return gross_if(leg, selection_wins=leg.selection == outcome)
        case LegResult.WON:
            return gross_if(leg, selection_wins=leg.side is Side.BACK)
        case LegResult.LOST:
            return gross_if(leg, selection_wins=leg.side is Side.LAY)
        case LegResult.VOID:
            return 0
        case LegResult.CASHED_OUT:
            assert leg.settled_amount is not None  # enforced by Leg validation
            return leg.settled_amount


def commission_by_venue(legs: Sequence[Leg]) -> dict[str, int]:
    """Commission per venue group, raising if legs in one group disagree (§14)."""
    if not legs:
        raise ValidationError("a position needs at least one leg")
    found: dict[str, int] = {}
    for leg in legs:
        known = found.setdefault(leg.venue_key, leg.commission_bp)
        if known != leg.commission_bp:
            raise ValidationError(
                f"legs at venue {leg.venue!r} disagree on commission "
                f"({known} vs {leg.commission_bp} bp)"
            )
    return found


def validate_position(legs: Sequence[Leg], market_outcomes: int = MIN_MARKET_OUTCOMES) -> None:
    """Raise :class:`ValidationError` unless ``legs`` form a well-formed position (§14)."""
    commission_by_venue(legs)
    if isinstance(market_outcomes, bool) or market_outcomes < MIN_MARKET_OUTCOMES:
        raise ValidationError(
            f"market_outcomes must be >= {MIN_MARKET_OUTCOMES}: {market_outcomes}"
        )
    for leg in legs:
        if leg.selection >= market_outcomes:
            raise ValidationError(
                f"selection index {leg.selection} is outside a {market_outcomes}-outcome market"
            )


def outcome_pl(legs: Sequence[Leg], outcome: int) -> Pence:
    """Net P/L of the position if ``outcome`` occurs, after per-venue commission netting (§5.3).

    Legs are grouped by :attr:`Leg.venue_key`; a group whose legs disagree on commission is a
    hard error, whatever the leg order.
    """
    commission = commission_by_venue(legs)
    gross_by_venue: defaultdict[str, int] = defaultdict(int)
    already_net = 0
    for leg in legs:
        if leg.result is LegResult.CASHED_OUT:
            already_net += leg_gross(leg, outcome)
        else:
            gross_by_venue[leg.venue_key] += leg_gross(leg, outcome)
    total = already_net
    with localcontext(DECIMAL_CONTEXT):
        for venue, gross in gross_by_venue.items():
            bp = commission[venue]
            charged = 0
            if gross > 0 and bp > 0:
                charged = to_pence(Decimal(gross) * Decimal(bp) / Decimal(10_000))
            total += gross - charged
    return total


def pl_vector(legs: Sequence[Leg], market_outcomes: int = MIN_MARKET_OUTCOMES) -> tuple[Pence, ...]:
    """P/L for every outcome ``0 .. market_outcomes - 1``."""
    validate_position(legs, market_outcomes)
    return tuple(outcome_pl(legs, k) for k in range(market_outcomes))


def guaranteed_pl(legs: Sequence[Leg], market_outcomes: int = MIN_MARKET_OUTCOMES) -> Pence:
    """The worst case across outcomes: what ``expected_pl_pence`` stores (§5.2)."""
    return min(pl_vector(legs, market_outcomes))


def realised_pl(legs: Sequence[Leg]) -> Pence | None:
    """Realised P/L once every leg has a result; ``None`` while any leg is PENDING (§8.2).

    Once every leg is a constant the vector is the same for every outcome, so no
    ``market_outcomes`` is needed; the commission-consistency rule still applies.
    """
    if not legs or any(not leg.is_settled for leg in legs):
        return None
    return outcome_pl(legs, 0)


def status_of(legs: Sequence[Leg]) -> BetStatus:
    """Derive the bet status from leg results (§9.1)."""
    if not legs:
        raise ValidationError("a bet needs at least one leg")
    settled = [leg for leg in legs if leg.is_settled]
    if not settled:
        return BetStatus.OPEN
    if len(settled) < len(legs):
        return BetStatus.PARTIALLY_SETTLED
    if all(leg.result is LegResult.VOID for leg in legs):
        return BetStatus.VOID
    return BetStatus.SETTLED


def open_liability(legs: Sequence[Leg]) -> Pence:
    """Exchange exposure still live: liabilities of PENDING lay legs (§5.2)."""
    return sum(leg.liability for leg in legs if leg.side is Side.LAY and not leg.is_settled)
