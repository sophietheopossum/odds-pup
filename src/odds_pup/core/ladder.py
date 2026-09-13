"""Exchange price ladders (tick sizes). Used for warnings only, never for hard validation.

SPEC §14. Betfair's ladder is the only one in v0.1; Smarkets and Matchbook are §16.2.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from odds_pup.core.money import DECIMAL_CONTEXT


@dataclass(frozen=True, slots=True)
class Band:
    """Inclusive odds range with a fixed tick size."""

    lower: Decimal
    upper: Decimal
    step: Decimal


@dataclass(frozen=True, slots=True)
class Ladder:
    """An ordered, contiguous set of bands. Band boundaries belong to both neighbours."""

    name: str
    bands: tuple[Band, ...]

    @property
    def minimum(self) -> Decimal:
        return self.bands[0].lower

    @property
    def maximum(self) -> Decimal:
        return self.bands[-1].upper

    def band_for(self, odds: Decimal) -> Band | None:
        return next((b for b in self.bands if b.lower <= odds <= b.upper), None)

    def is_tick(self, odds: Decimal) -> bool:
        """True if ``odds`` is a price that can actually be placed on this exchange."""
        band = self.band_for(odds)
        if band is None:
            return False
        with localcontext(DECIMAL_CONTEXT):
            return (odds - band.lower) % band.step == 0

    def nearest_ticks(self, odds: Decimal) -> tuple[Decimal | None, Decimal | None]:
        """The ladder prices at or just below and at or just above ``odds``.

        Either side is ``None`` when ``odds`` lies outside the ladder's range.
        """
        if odds < self.minimum:
            return None, self.minimum
        if odds > self.maximum:
            return self.maximum, None
        band = self.band_for(odds)
        assert band is not None  # contiguous bands cover [minimum, maximum]
        with localcontext(DECIMAL_CONTEXT):
            steps = (odds - band.lower) // band.step
            below = band.lower + steps * band.step
            above = below if below == odds else below + band.step
        return below, above


def _band(lower: str, upper: str, step: str) -> Band:
    return Band(Decimal(lower), Decimal(upper), Decimal(step))


BETFAIR = Ladder(
    "BETFAIR",
    (
        _band("1.01", "2", "0.01"),
        _band("2", "3", "0.02"),
        _band("3", "4", "0.05"),
        _band("4", "6", "0.1"),
        _band("6", "10", "0.2"),
        _band("10", "20", "0.5"),
        _band("20", "30", "1"),
        _band("30", "50", "2"),
        _band("50", "100", "5"),
        _band("100", "1000", "10"),
    ),
)

LADDERS: dict[str, Ladder] = {BETFAIR.name: BETFAIR}
