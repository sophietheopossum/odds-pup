"""Money as integer pence, with the single rounding rule the whole application shares.

See SPEC §4. Nothing in this package ever touches ``float``.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext

from odds_pup.core.errors import CoreError

type Pence = int
"""An amount of money in whole pence. Negative values are losses."""

DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_UP)
"""Arithmetic context for every calculation in ``core``."""

PENCE_PER_POUND = 100

MAX_MONEY_PENCE = 10**12
"""£10,000,000,000. A guard that keeps every product exact at precision 28 (SPEC §14)."""

_MONEY_RE = re.compile(
    r"""
    ^\s*
    (?P<sign_a>[-+])?          # sign before the symbol
    (?:£)?                     # optional pound sign
    (?P<sign_b>[-+])?          # or sign after the symbol
    (?P<whole>[0-9]+)
    (?:\.(?P<frac>[0-9]{1,2}))?   # at most two decimal places
    \s*$
    """,
    re.VERBOSE | re.ASCII,
)


class MoneyError(CoreError):
    """Raised when text cannot be read as a money amount."""


def to_pence(value: Decimal) -> Pence:
    """Round an exact Decimal pence value to whole pence, ROUND_HALF_UP.

    This is the only place in ``core`` that rounds money. Callers pass a value already
    expressed in pence (for example ``stake_pence * (odds - 1)``).
    """
    with localcontext(DECIMAL_CONTEXT):
        return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def pounds(amount: Pence) -> Decimal:
    """Express pence as an exact two-decimal-place Decimal number of pounds."""
    with localcontext(DECIMAL_CONTEXT):
        return Decimal(amount).scaleb(-2)


def parse_money(text: str) -> Pence:
    """Parse ``"10"``, ``"10.5"``, ``"£10.00"``, ``"-0.58"`` or ``"-£0.58"`` into pence.

    Rejects thousands separators, comma decimals, more than two decimal places, non-ASCII
    digits, amounts beyond :data:`MAX_MONEY_PENCE` and a sign on both sides of the symbol
    (SPEC §12, §14).
    """
    match = _MONEY_RE.match(text)
    if match is None:
        raise MoneyError(f"not a money amount: {text!r}")
    if match["sign_a"] and match["sign_b"]:
        raise MoneyError(f"two signs in money amount: {text!r}")
    sign = -1 if (match["sign_a"] or match["sign_b"]) == "-" else 1
    frac = (match["frac"] or "").ljust(2, "0")
    magnitude = int(match["whole"]) * PENCE_PER_POUND + int(frac)
    if magnitude > MAX_MONEY_PENCE:
        raise MoneyError(f"amount too large: {text!r}")
    return sign * magnitude


def format_pence(amount: Pence, symbol: str = "£") -> str:
    """Format pence as ``£12.34`` or ``-£0.58`` (sign first, always two decimals)."""
    sign = "-" if amount < 0 else ""
    whole, frac = divmod(abs(amount), PENCE_PER_POUND)
    return f"{sign}{symbol}{whole}.{frac:02d}"


def format_signed_pence(amount: Pence, symbol: str = "£") -> str:
    """Like :func:`format_pence` but with an explicit ``+`` for gains, for P/L columns.

    SPEC §13: the sign must be carried textually; colour is secondary.
    """
    return f"+{format_pence(amount, symbol)}" if amount > 0 else format_pence(amount, symbol)
