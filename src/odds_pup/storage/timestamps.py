"""UTC storage text and Europe/London bucketing. SPEC §12."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from odds_pup.storage.errors import StorageError

UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
LOCAL_ZONE = ZoneInfo("Europe/London")
_UTC_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", re.ASCII)


def utc_now() -> datetime:
    """The current instant, timezone-aware UTC, whole seconds."""
    return datetime.now(UTC).replace(microsecond=0)


def to_db(moment: datetime) -> str:
    """Fixed-width UTC text with an explicit ``Z`` (never ``isoformat()``'s ``+00:00``)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise StorageError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).replace(microsecond=0).strftime(UTC_FORMAT)


def from_db(text: str) -> datetime:
    """Parse the stored form back into an aware UTC datetime."""
    if _UTC_TEXT.match(text) is None:
        raise StorageError(f"not a stored UTC timestamp: {text!r}")
    return datetime.strptime(text, UTC_FORMAT).replace(tzinfo=UTC)


def to_local(moment: datetime) -> datetime:
    return moment.astimezone(LOCAL_ZONE)


def local_date(moment: datetime) -> date:
    """The calendar date in Europe/London: what monthly summaries bucket by."""
    return to_local(moment).date()


def local_month_bounds(moment: datetime) -> tuple[datetime, datetime]:
    """UTC instants for the start of ``moment``'s local month and of the next local month.

    A bet settled at 00:30 BST on 1 September is 23:30Z on 31 August and must count in
    September; querying ``settled_at >= start AND settled_at < end`` with these bounds does that.
    """
    local = to_local(moment)
    start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (start_local + timedelta(days=32)).replace(day=1)
    return start_local.astimezone(UTC), next_month.astimezone(UTC)
