"""UTC storage text and Europe/London bucketing. SPEC §12."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from odds_pup.core import ValidationError
from odds_pup.storage.errors import StorageError

UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
LOCAL_ZONE = ZoneInfo("Europe/London")
_UTC_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", re.ASCII)


def utc_now() -> datetime:
    """The current instant, timezone-aware UTC, whole seconds."""
    return datetime.now(UTC).replace(microsecond=0)


def require_aware(moment: object, name: str = "timestamp") -> datetime:
    """Return ``moment`` as whole-second UTC, or raise ValidationError if it is not aware.

    Naive datetimes are refused rather than guessed at: Qt hands back naive values and
    interpreting them in the system zone would silently mis-bucket bets.
    """
    if not isinstance(moment, datetime):
        raise ValidationError(f"{name} must be a datetime, not {type(moment).__name__}")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValidationError(f"{name} must be timezone-aware")
    return moment.astimezone(UTC).replace(microsecond=0)


def to_db(moment: datetime) -> str:
    """Fixed-width UTC text with an explicit ``Z`` (never ``isoformat()``'s ``+00:00``)."""
    return require_aware(moment).strftime(UTC_FORMAT)


def from_db(text: str) -> datetime:
    """Parse the stored form back into an aware UTC datetime."""
    if not isinstance(text, str) or _UTC_TEXT.match(text) is None:
        raise StorageError(f"not a stored UTC timestamp: {text!r}")
    try:
        return datetime.strptime(text, UTC_FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        raise StorageError(f"not a valid UTC timestamp: {text!r}") from exc


def to_local(moment: datetime) -> datetime:
    return require_aware(moment).astimezone(LOCAL_ZONE)


def local_date(moment: datetime) -> date:
    """The calendar date in Europe/London: what monthly summaries bucket by."""
    return to_local(moment).date()


def local_day_start(day: date) -> datetime:
    """The UTC instant at which ``day`` begins in Europe/London."""
    if isinstance(day, datetime) or not isinstance(day, date):
        raise ValidationError(f"expected a calendar date, not {type(day).__name__}")
    return datetime.combine(day, time.min, tzinfo=LOCAL_ZONE).astimezone(UTC)


def local_date_range(first: date, last: date) -> tuple[datetime, datetime]:
    """UTC bounds for the local days ``first``..``last`` inclusive, as ``[start, end)``.

    Use these for :class:`~odds_pup.storage.BetFilter` ``placed_from`` / ``placed_to``.
    """
    return local_day_start(first), local_day_start(last + timedelta(days=1))


def local_month_bounds(moment: datetime) -> tuple[datetime, datetime]:
    """UTC instants for the start of ``moment``'s local month and of the next local month.

    A bet settled at 00:30 BST on 1 September is 23:30Z on 31 August and must count in
    September; querying ``settled_at >= start AND settled_at < end`` with these bounds does that.
    """
    local = to_local(moment)
    first = local.date().replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    return local_day_start(first), local_day_start(following)
