from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from odds_pup.core import ValidationError
from odds_pup.storage import (
    StorageError,
    from_db,
    local_date,
    local_month_bounds,
    to_db,
    to_local,
    utc_now,
)


def test_to_db_is_fixed_width_with_z():
    moment = datetime(2026, 9, 1, 0, 30, 15, 123456, tzinfo=timezone(timedelta(hours=1)))
    assert to_db(moment) == "2026-08-31T23:30:15Z"


def test_from_db_round_trips_and_rejects_other_forms():
    assert from_db("2026-08-31T23:30:15Z") == datetime(2026, 8, 31, 23, 30, 15, tzinfo=UTC)
    assert to_db(from_db("2026-08-31T23:30:15Z")) == "2026-08-31T23:30:15Z"
    for bad in ("2026-08-31T23:30:15+00:00", "2026-08-31 23:30:15Z", "2026-08-31T23:30Z", ""):
        with pytest.raises(StorageError):
            from_db(bad)


def test_naive_datetimes_are_rejected_everywhere():
    naive = datetime(2026, 9, 1, 0, 30)
    for fn in (to_db, to_local, local_date, local_month_bounds):
        with pytest.raises(ValidationError):
            fn(naive)
    with pytest.raises(ValidationError):
        to_db("2026-09-01T00:30:00Z")  # type: ignore[arg-type]


def test_from_db_rejects_impossible_dates_as_storage_errors():
    for bad in ("2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z", "2026-09-12T25:00:00Z"):
        with pytest.raises(StorageError):
            from_db(bad)


def test_local_day_helpers_follow_london_time():
    from datetime import date

    from odds_pup.storage import local_date_range, local_day_start

    assert to_db(local_day_start(date(2026, 9, 2))) == "2026-09-01T23:00:00Z"
    assert to_db(local_day_start(date(2026, 1, 2))) == "2026-01-02T00:00:00Z"
    start, end = local_date_range(date(2026, 10, 24), date(2026, 10, 25))  # clocks go back 25 Oct
    assert (to_db(start), to_db(end)) == ("2026-10-23T23:00:00Z", "2026-10-26T00:00:00Z")
    with pytest.raises(ValidationError):
        local_day_start(datetime(2026, 9, 2, tzinfo=UTC))


def test_utc_now_is_aware_whole_seconds():
    now = utc_now()
    assert now.tzinfo is UTC
    assert now.microsecond == 0


def test_local_date_uses_europe_london():
    # 23:30Z on 31 Aug is 00:30 BST on 1 Sep
    assert local_date(datetime(2026, 8, 31, 23, 30, tzinfo=UTC)) == date(2026, 9, 1)
    # in winter London is on UTC
    assert local_date(datetime(2026, 1, 31, 23, 30, tzinfo=UTC)) == date(2026, 1, 31)


def test_local_month_bounds_in_summer_and_winter():
    start, end = local_month_bounds(datetime(2026, 9, 15, 12, tzinfo=UTC))
    assert to_db(start) == "2026-08-31T23:00:00Z"
    assert to_db(end) == "2026-09-30T23:00:00Z"
    start, end = local_month_bounds(datetime(2026, 1, 15, 12, tzinfo=UTC))
    assert to_db(start) == "2026-01-01T00:00:00Z"
    assert to_db(end) == "2026-02-01T00:00:00Z"
    # the moment just inside the local month at 00:30 BST on 1 Sep is 23:30Z on 31 Aug
    start, _ = local_month_bounds(datetime(2026, 8, 31, 23, 30, tzinfo=UTC))
    assert to_db(start) == "2026-08-31T23:00:00Z"


def test_to_local_offset():
    assert to_local(datetime(2026, 7, 1, 12, tzinfo=UTC)).utcoffset() == timedelta(hours=1)
