from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from odds_pup.core import BETFAIR, OddsError, format_odds, parse_odds, storage_form, validate_odds
from odds_pup.core.odds import decimal_places


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.01", "1.01"),
        ("2", "2"),
        ("2.375", "2.375"),
        ("1000", "1000"),
        ("1/1", "2"),
        ("6/4", "2.5"),
        ("10/11", "1.9091"),
        ("1/1000", "1.001"),
        ("11 / 8", "2.375"),
        ("Evens", "2"),
        ("9/1", "10"),
        ("99/1", "100"),
        ("1/19999", "1.0001"),
        ("10000", "10000"),
    ],
)
def test_parse_odds(text, expected):
    assert parse_odds(text) == Decimal(expected)


def test_parse_odds_quantizes_fractions_to_four_places():
    assert parse_odds("100/30") == Decimal("4.3333")
    assert parse_odds("1/3") == Decimal("1.3333")
    assert parse_odds("2/3") == Decimal("1.6667")


@pytest.mark.parametrize(
    "value", ["1", "0.99", "-3", "2.00001", "NaN", "Infinity", "10000.0001", "1E+5"]
)
def test_validate_odds_rejects(value):
    with pytest.raises(OddsError):
        validate_odds(Decimal(value))


def test_validate_odds_rejects_floats():
    with pytest.raises(OddsError):
        validate_odds(2.1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [
        "1/100000",
        "\u0661\u0661/\u0668",
        "\uff12.\uff15",
        "10001",
        "1" + "0" * 30,
        ("1" + "0" * 30) + "/1",
        "1/0",
    ],
)
def test_parse_odds_rejects_out_of_range_and_non_ascii(text):
    with pytest.raises(OddsError):
        parse_odds(text)


def test_whole_number_fractions_never_come_back_in_exponent_form():
    ten = parse_odds("9/1")
    assert str(ten) == "10"
    assert format_odds(ten) == "10.00"
    assert storage_form(ten) == "10.0000"
    assert BETFAIR.is_tick(ten)
    assert str(parse_odds("5/2")) == "3.5"
    assert str(parse_odds("3/2")) == "2.5"
    assert str(parse_odds("199/1")) == "200"


def test_format_and_storage_cope_with_exponent_form_input():
    assert format_odds(Decimal("1E+1")) == "10.00"
    assert storage_form(Decimal("1E+1")) == "10.0000"


@pytest.mark.parametrize(
    ("odds", "text"),
    [("2", "2.00"), ("2.1", "2.10"), ("2.375", "2.375"), ("4.3333", "4.3333"), ("1000", "1000.00")],
)
def test_format_odds(odds, text):
    assert format_odds(Decimal(odds)) == text


def test_storage_form_is_fixed_four_places():
    assert storage_form(Decimal("2.1")) == "2.1000"
    assert storage_form(Decimal("1000")) == "1000.0000"
    assert Decimal(storage_form(Decimal("2.375"))) == Decimal("2.375")
    with pytest.raises(OddsError):
        storage_form(Decimal("1"))


def test_decimal_places():
    assert decimal_places(Decimal("2")) == 0
    assert decimal_places(Decimal("2.10")) == 2
    assert decimal_places(Decimal("2.1000")) == 4
    assert decimal_places(Decimal("1E+1")) == 0


@given(
    st.decimals(min_value=Decimal("1.0001"), max_value=Decimal("10000"), places=4, allow_nan=False)
)
def test_odds_survive_format_and_storage(odds):
    assert parse_odds(format_odds(odds)) == odds
    assert parse_odds(storage_form(odds)) == odds
    assert "E" not in str(parse_odds(format_odds(odds)))
