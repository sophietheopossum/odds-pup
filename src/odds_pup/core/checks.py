"""Advisory warnings for the New Bet panel. SPEC §14 (warnings, not errors)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from odds_pup.core.commission import format_commission
from odds_pup.core.model import BetType
from odds_pup.core.money import Pence, format_pence
from odds_pup.core.odds import format_odds
from odds_pup.core.templates import exact_rating

if TYPE_CHECKING:
    from odds_pup.core.ladder import Ladder
    from odds_pup.core.templates import TwoWayPosition

HIGH_COMMISSION_BP = 1000
LOW_QUALIFIER_RATING = Decimal(80)


class AdvisoryCode(StrEnum):
    LAY_ODDS_OFF_LADDER = "LAY_ODDS_OFF_LADDER"
    LAY_STAKE_BELOW_MINIMUM = "LAY_STAKE_BELOW_MINIMUM"
    HIGH_COMMISSION = "HIGH_COMMISSION"
    LOW_RATING = "LOW_RATING"


@dataclass(frozen=True, slots=True)
class Advisory:
    code: AdvisoryCode
    message: str


def advisories_for(
    position: TwoWayPosition,
    *,
    ladder: Ladder | None = None,
    min_lay_stake: Pence | None = None,
) -> list[Advisory]:
    """Warnings worth showing before saving; the position is still valid."""
    found: list[Advisory] = []
    lay = position.lay
    if ladder is not None and not ladder.is_tick(lay.odds):
        below, above = ladder.nearest_ticks(lay.odds)
        options = " or ".join(format_odds(t) for t in (below, above) if t is not None)
        found.append(
            Advisory(
                AdvisoryCode.LAY_ODDS_OFF_LADDER,
                f"{format_odds(lay.odds)} is not a {ladder.name.title()} price; "
                f"nearest: {options}.",
            )
        )
    if min_lay_stake is not None and 0 < lay.stake < min_lay_stake:
        found.append(
            Advisory(
                AdvisoryCode.LAY_STAKE_BELOW_MINIMUM,
                f"Lay stake {format_pence(lay.stake)} is below the exchange minimum "
                f"{format_pence(min_lay_stake)}.",
            )
        )
    if lay.commission_bp > HIGH_COMMISSION_BP:
        found.append(
            Advisory(
                AdvisoryCode.HIGH_COMMISSION,
                f"Commission of {format_commission(lay.commission_bp)} is unusually high.",
            )
        )
    if position.bet_type is BetType.QUALIFYING:
        precise = exact_rating(position.bet_type, position.back.odds, lay.odds, lay.commission_bp)
        if precise < LOW_QUALIFIER_RATING:
            found.append(
                Advisory(
                    AdvisoryCode.LOW_RATING,
                    f"Rating {position.rating}% is low for a qualifier; check the odds.",
                )
            )
    return found
