from __future__ import annotations

from decimal import Decimal

import pytest

from odds_pup.core import ValidationError, commission_fraction, format_commission, parse_commission


def test_commission_fraction_is_exact():
    assert commission_fraction(200) == Decimal("0.02")
    assert commission_fraction(0) == 0
    assert commission_fraction(9999) == Decimal("0.9999")
    with pytest.raises(ValidationError):
        commission_fraction(10000)


@pytest.mark.parametrize(
    ("text", "bp"),
    [
        ("2", 200),
        ("2%", 200),
        ("2.5", 250),
        ("2.5 %", 250),
        (" 0 ", 0),
        ("0.01", 1),
        ("99.99", 9999),
    ],
)
def test_parse_commission_accepts(text, bp):
    assert parse_commission(text) == bp


@pytest.mark.parametrize("text", ["100", "100%", "2,5", "2.555", "-1", "", "%", "abc", "\u0662"])
def test_parse_commission_rejects(text):
    with pytest.raises(ValidationError):
        parse_commission(text)


@pytest.mark.parametrize(
    ("bp", "text"), [(200, "2%"), (250, "2.5%"), (0, "0%"), (1000, "10%"), (1234, "12.34%")]
)
def test_format_commission(bp, text):
    assert format_commission(bp) == text
    assert parse_commission(text) == bp
