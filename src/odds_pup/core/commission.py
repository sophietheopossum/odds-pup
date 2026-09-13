"""Exchange commission: basis points in the model, percent text at the UI edge. SPEC §4, §12."""

from __future__ import annotations

import re
from decimal import Decimal, localcontext

from odds_pup.core.model import MAX_COMMISSION_BP, ValidationError, validate_commission_bp
from odds_pup.core.money import DECIMAL_CONTEXT

_PERCENT_RE = re.compile(
    r"^\s*(?P<whole>[0-9]+)(?:\.(?P<frac>[0-9]{1,2}))?\s*%?\s*$",
    re.ASCII,
)


def commission_fraction(commission_bp: int) -> Decimal:
    """Basis points to an exact Decimal fraction (200 -> Decimal('0.02'))."""
    with localcontext(DECIMAL_CONTEXT):
        return Decimal(validate_commission_bp(commission_bp)).scaleb(-4)


def parse_commission(text: str) -> int:
    """Parse ``"2"``, ``"2%"``, ``"2.5 %"`` or ``"0"`` into basis points.

    At most two decimal places (one basis point), ASCII digits only, no comma decimals, and the
    result must be below 100% (SPEC §14).
    """
    match = _PERCENT_RE.match(text)
    if match is None:
        raise ValidationError(f"not a commission percentage: {text!r}")
    frac = (match["frac"] or "").ljust(2, "0")
    bp = int(match["whole"]) * 100 + int(frac)
    if bp > MAX_COMMISSION_BP:
        raise ValidationError(f"commission must be below 100%: {text!r}")
    return bp


def format_commission(commission_bp: int) -> str:
    """Basis points as percent text: 200 -> ``"2%"``, 250 -> ``"2.5%"``, 0 -> ``"0%"``."""
    validate_commission_bp(commission_bp)
    with localcontext(DECIMAL_CONTEXT):
        percent = Decimal(commission_bp).scaleb(-2)
        text = format(percent.normalize(), "f") if percent else "0"
    return f"{text}%"
