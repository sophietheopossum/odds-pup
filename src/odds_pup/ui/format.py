"""Display formatting for the UI: local times, money with textual signs, labels. SPEC §12-§13."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from PySide6.QtCore import QDateTime

from odds_pup.core import (
    BetStatus,
    BetType,
    LegResult,
    Pence,
    StakeKind,
    format_commission,
    format_odds,
    format_signed_pence,
)
from odds_pup.storage import AuditKind, AuditValueKind, audit_value_kind, from_db, to_local

DATE_FORMAT = "%d/%m/%Y"
DATETIME_FORMAT = "%d/%m/%Y %H:%M"

TYPE_LABELS: dict[BetType, str] = {
    BetType.QUALIFYING: "Qualifying",
    BetType.FREE_BET_SNR: "Free bet (SNR)",
    BetType.FREE_BET_SR: "Free bet (SR)",
    BetType.DUTCHING: "Dutching",
}
TYPE_SHORT: dict[BetType, str] = {
    BetType.QUALIFYING: "QL",
    BetType.FREE_BET_SNR: "SNR",
    BetType.FREE_BET_SR: "SR",
    BetType.DUTCHING: "Dutch",
}
STATUS_LABELS: dict[BetStatus, str] = {
    BetStatus.OPEN: "Open",
    BetStatus.PARTIALLY_SETTLED: "Part-settled",
    BetStatus.SETTLED: "Settled",
    BetStatus.VOID: "Void",
}
RESULT_LABELS: dict[LegResult, str] = {
    LegResult.PENDING: "Pending",
    LegResult.WON: "Won",
    LegResult.LOST: "Lost",
    LegResult.VOID: "Void",
    LegResult.CASHED_OUT: "Cashed out",
}


def local_datetime_text(moment: datetime | None) -> str:
    return "" if moment is None else to_local(moment).strftime(DATETIME_FORMAT)


def local_date_text(moment: datetime | None) -> str:
    return "" if moment is None else to_local(moment).strftime(DATE_FORMAT)


def pl_text(amount: Pence | None) -> str:
    """P/L for a cell: explicit sign, empty when unknown (SPEC §13: sign is textual)."""
    return "" if amount is None else format_signed_pence(amount)


def to_qdatetime(moment: datetime) -> QDateTime:
    """Aware datetime -> QDateTime in the machine's local zone, via epoch seconds."""
    return QDateTime.fromSecsSinceEpoch(int(moment.timestamp()))


def from_qdatetime(value: QDateTime) -> datetime:
    """QDateTime -> aware UTC datetime, whole seconds."""
    return datetime.fromtimestamp(value.toSecsSinceEpoch(), UTC)


AUDIT_KIND_LABELS: dict[str, str] = {
    "CREATED": "Created",
    "EDITED": "Edited",
    "SETTLED": "Settled",
    "REOPENED": "Reopened",
    "ADJUSTMENT": "Adjusted",
    "CORRECTION": "Corrected",
    "DELETED": "Deleted",
    "RESTORED": "Restored",
}
ENUM_LABELS: dict[str, str] = {
    **{r.value: label for r, label in RESULT_LABELS.items()},
    **{s.value: label for s, label in STATUS_LABELS.items()},
    "BACK": "Back",
    "LAY": "Lay",
    StakeKind.CASH.value: "Cash",
    StakeKind.FREE_SNR.value: "Free bet (SNR)",
    StakeKind.FREE_SR.value: "Free bet (SR)",
    **{t.value: label for t, label in TYPE_LABELS.items()},
}
_FIELD_LABELS: dict[str, str] = {
    "bet_type": "Type",
    "event_name": "Event",
    "selection": "Selection",
    "market": "Market",
    "market_outcomes": "Outcomes",
    "offer_id": "Offer",
    "parent_bet_id": "Parent bet",
    "placed_at": "Placed",
    "event_at": "Event start",
    "settled_at": "Settled",
    "deleted_at": "Deleted",
    "notes": "Notes",
    "needs_review": "Needs review",
    "status": "Status",
    "expected_pl_pence": "Expected P/L",
    "actual_pl_override_pence": "Adjusted P/L",
    "side": "side",
    "venue": "venue",
    "odds": "odds",
    "stake": "stake",
    "stake_kind": "stake kind",
    "commission_bp": "commission",
    "odds_text": "odds as typed",
    "result": "result",
    "settled_amount": "cash-out net",
}


def audit_kind_text(kind: AuditKind) -> str:
    return AUDIT_KIND_LABELS.get(kind.value, kind.value.title())


def audit_field_text(field: str | None) -> str:
    """``legs[1].stake`` -> ``Leg 2 stake``; header fields get plain names."""
    if field is None:
        return ""
    if field.startswith("legs["):
        index_text, _, rest = field[5:].partition("]")
        label = f"Leg {int(index_text) + 1}"
        attribute = rest.lstrip(".")
        return f"{label} {_FIELD_LABELS.get(attribute, attribute)}".strip()
    return _FIELD_LABELS.get(field, field)


def audit_value_text(field: str | None, value: str | None) -> str:
    """Render an audit row's stored text the way the rest of the UI shows that kind of value."""
    if value is None:
        return ""
    kind = audit_value_kind(field)
    try:
        match kind:
            case AuditValueKind.MONEY:
                return format_signed_pence(int(value))
            case AuditValueKind.ODDS:
                return format_odds(Decimal(value))
            case AuditValueKind.COMMISSION:
                return format_commission(int(value))
            case AuditValueKind.TIMESTAMP:
                return local_datetime_text(from_db(value))
            case AuditValueKind.FLAG:
                return "Yes" if value == "1" else "No"
            case AuditValueKind.COUNT if field and field.endswith(".selection"):
                return f"selection {int(value) + 1}"
            case AuditValueKind.ENUM:
                return ENUM_LABELS.get(value, value.title())
            case _:
                return {"backfill": "backfilled", "present": "present", "removed": "removed"}.get(
                    value, value
                )
    except (ValueError, ArithmeticError):
        return value
