from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from odds_pup.core import (
    MAX_MONEY_PENCE,
    MoneyError,
    format_pence,
    format_signed_pence,
    parse_money,
    pounds,
    to_pence,
)


@pytest.mark.parametrize(
    ("text", "pence"),
    [
        ("10", 1000),
        ("10.5", 1050),
        ("10.00", 1000),
        ("£10.00", 1000),
        ("-0.58", -58),
        ("-£0.58", -58),
        ("£-0.58", -58),
        ("+2", 200),
        (" 3.07 ", 307),
        ("0", 0),
        ("-0", 0),
        ("£+2", 200),
        ("+£2", 200),
        ("£0.5", 50),
        ("00010", 1000),
    ],
)
def test_parse_money_accepts(text, pence):
    assert parse_money(text) == pence


@pytest.mark.parametrize(
    "text",
    [
        "1,000",
        "10,50",
        "10.005",
        "",
        "£",
        "abc",
        "-£-1",
        "1e3",
        "£ 10",
        "10.",
        ".5",
        "\u0661\u0660",  # Arabic-Indic digits
        "\uff11\uff10",  # fullwidth digits
        "10000000001",  # above MAX_MONEY_PENCE
    ],
)
def test_parse_money_rejects(text):
    with pytest.raises(MoneyError):
        parse_money(text)


def test_parse_money_bound_is_inclusive():
    assert parse_money("10000000000") == MAX_MONEY_PENCE


@pytest.mark.parametrize(
    ("pence", "text"),
    [(1234, "£12.34"), (-58, "-£0.58"), (0, "£0.00"), (5, "£0.05"), (100000, "£1000.00")],
)
def test_format_pence(pence, text):
    assert format_pence(pence) == text


def test_format_signed_pence_marks_gains():
    assert format_signed_pence(471) == "+£4.71"
    assert format_signed_pence(-58) == "-£0.58"
    assert format_signed_pence(0) == "£0.00"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0.5", 1), ("1.5", 2), ("2.5", 3), ("-0.5", -1), ("-1.5", -2), ("19.24", 19), ("19.5", 20)],
)
def test_to_pence_rounds_half_up_away_from_zero(value, expected):
    assert to_pence(Decimal(value)) == expected


def test_pounds_is_exact():
    assert pounds(-58) == Decimal("-0.58")
    assert str(pounds(1000)) == "10.00"
    assert str(pounds(MAX_MONEY_PENCE)) == "10000000000.00"


@given(st.integers(min_value=-MAX_MONEY_PENCE, max_value=MAX_MONEY_PENCE))
def test_money_round_trips(pence):
    assert parse_money(format_pence(pence)) == pence
    assert parse_money(format_signed_pence(pence)) == pence
