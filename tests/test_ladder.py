from __future__ import annotations

from decimal import Decimal

import pytest

from odds_pup.core import BETFAIR

D = Decimal


@pytest.mark.parametrize(
    "odds",
    [
        "1.01", "1.99", "2", "2.02", "3", "3.05", "4", "4.1", "6", "6.2", "10", "10.5",
        "20", "21", "30", "32", "50", "55", "100", "110", "1000", "1E+1",
    ],
)  # fmt: skip
def test_betfair_ticks(odds):
    assert BETFAIR.is_tick(D(odds))


@pytest.mark.parametrize(
    "odds",
    [
        "1.005",
        "2.01",
        "2.03",
        "3.01",
        "4.05",
        "6.1",
        "10.2",
        "20.5",
        "31",
        "51",
        "105",
        "1001",
        "1",
    ],
)
def test_betfair_non_ticks(odds):
    assert not BETFAIR.is_tick(D(odds))


@pytest.mark.parametrize(
    ("odds", "below", "above"),
    [
        ("2.01", "2", "2.02"),
        ("2.10", "2.10", "2.10"),
        ("3.01", "3", "3.05"),
        ("4.05", "4", "4.1"),
        ("6.1", "6", "6.2"),
        ("31", "30", "32"),
        ("105", "100", "110"),
        ("2", "2", "2"),
        ("3", "3", "3"),
        ("1.01", "1.01", "1.01"),
        ("1000", "1000", "1000"),
        ("1.995", "1.99", "2"),
        ("3.999", "3.95", "4"),
        ("999.9999", "990", "1000"),
        ("1E+1", "10", "10"),
    ],
)
def test_nearest_ticks(odds, below, above):
    result = BETFAIR.nearest_ticks(D(odds))
    assert result == (D(below), D(above))
    assert all(t is not None and BETFAIR.is_tick(t) for t in result)


def test_nearest_ticks_outside_range():
    assert BETFAIR.nearest_ticks(D("1.005")) == (None, D("1.01"))
    assert BETFAIR.nearest_ticks(D("1001")) == (D("1000"), None)


def test_ladder_is_contiguous():
    for left, right in zip(BETFAIR.bands, BETFAIR.bands[1:], strict=False):
        assert left.upper == right.lower
