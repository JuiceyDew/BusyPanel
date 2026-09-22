"""Money parsing and formatting. Floats are never used for money."""

from __future__ import annotations

import pytest

from busypanel.money import fmt_cents, parse_cents


@pytest.mark.parametrize("text,cents", [
    ("150", 15000),
    ("150.5", 15050),
    ("$1,200.50", 120050),
    ("-20", -2000),
    ("0", 0),
    ("0.05", 5),
    ("$ 99", 9900),
])
def test_parse_cents(text, cents):
    assert parse_cents(text) == cents


@pytest.mark.parametrize("bad", ["abc", "", "   ", "1.234", "1-2", "--5", "1.2.3", "$"])
def test_parse_cents_rejects(bad):
    with pytest.raises(ValueError):
        parse_cents(bad)


def test_fmt_cents():
    assert fmt_cents(120050) == "$1,200.50"
    assert fmt_cents(0) == "$0.00"
    assert fmt_cents(-2000) == "-$20.00"
    assert fmt_cents(5) == "$0.05"


def test_roundtrip():
    for cents in (0, 5, 100, 99999, -12345):
        assert parse_cents(fmt_cents(cents)) == cents
