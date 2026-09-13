"""Decimal odds: parsing (decimal or fractional text), validation and formatting. SPEC §4."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

from odds_pup.core.errors import CoreError
from odds_pup.core.money import DECIMAL_CONTEXT

MAX_ODDS_DECIMAL_PLACES = 4
MAX_ODDS = Decimal(10_000)
"""Above any real price (Betfair's ladder stops at 1000); keeps arithmetic exact (SPEC §14)."""

_QUANTUM = Decimal(1).scaleb(-MAX_ODDS_DECIMAL_PLACES)  # Decimal('0.0001')
_ONE = Decimal(1)

_DECIMAL_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$", re.ASCII)
_FRACTION_RE = re.compile(r"^(?P<num>[0-9]+)\s*/\s*(?P<den>[0-9]+)$", re.ASCII)
_EVENS = frozenset({"evens", "evs", "even"})


class OddsError(CoreError):
    """Raised when a value is not valid decimal odds."""


def decimal_places(value: Decimal) -> int:
    """Number of digits after the decimal point in ``value``'s representation."""
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):  # NaN / Infinity carry a string exponent
        raise OddsError(f"odds must be finite: {value}")
    return max(0, -exponent)


def validate_odds(odds: Decimal) -> Decimal:
    """Return ``odds`` if it is a finite Decimal in (1, MAX_ODDS] with at most four decimals."""
    if not isinstance(odds, Decimal):
        raise OddsError(f"odds must be a Decimal, not {type(odds).__name__}")
    if not odds.is_finite():
        raise OddsError(f"odds must be finite: {odds}")
    if odds <= 1:
        raise OddsError(f"odds must be greater than 1: {format_odds(odds)}")
    if odds > MAX_ODDS:
        raise OddsError(f"odds must be at most {MAX_ODDS}: {format_odds(odds)}")
    if decimal_places(odds) > MAX_ODDS_DECIMAL_PLACES:
        raise OddsError(
            f"odds may have at most {MAX_ODDS_DECIMAL_PLACES} decimal places: {format_odds(odds)}"
        )
    return odds


def _without_trailing_zeros(value: Decimal) -> Decimal:
    """Drop trailing fractional zeros without ever producing exponent form (``1E+1``)."""
    if value == value.to_integral_value():
        return value.quantize(_ONE)
    return value.normalize()


def parse_odds(text: str) -> Decimal:
    """Parse decimal odds (``"2.1"``), fractional odds (``"11/8"``) or ``"evens"``.

    Keywords match case-insensitively after trimming. Fractions convert as ``1 + num/den``
    quantized to four places ROUND_HALF_UP, so a non-terminating price such as ``100/30``
    becomes ``4.3333`` (SPEC §4 documents the consequence). Comma decimals, non-ASCII digits and
    more than four decimal places are rejected.
    """
    cleaned = text.strip().lower()
    if cleaned in _EVENS:
        return Decimal(2)
    if (match := _FRACTION_RE.match(cleaned)) is not None:
        numerator, denominator = int(match["num"]), int(match["den"])
        if denominator == 0:
            raise OddsError(f"fractional odds with zero denominator: {text!r}")
        if numerator == 0:
            raise OddsError(f"fractional odds must be greater than 0/1: {text!r}")
        with localcontext(DECIMAL_CONTEXT):
            try:
                value = _ONE + Decimal(numerator) / Decimal(denominator)
                value = value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)
            except InvalidOperation as exc:
                raise OddsError(f"fractional odds out of range: {text!r}") from exc
            return validate_odds(_without_trailing_zeros(value))
    if _DECIMAL_RE.match(cleaned) is None:
        raise OddsError(f"not decimal or fractional odds: {text!r}")
    return validate_odds(Decimal(cleaned))


def format_odds(odds: Decimal) -> str:
    """Canonical display form: at least two decimal places, trailing zeros beyond that dropped."""
    text = format(odds.normalize(), "f") if odds.is_finite() else str(odds)
    whole, _, frac = text.partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"


def storage_form(odds: Decimal) -> str:
    """The TEXT form stored in SQLite: fixed four decimal places (``'2.1000'``)."""
    with localcontext(DECIMAL_CONTEXT):
        return format(validate_odds(odds).quantize(_QUANTUM), "f")
