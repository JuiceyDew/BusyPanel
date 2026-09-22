"""Money as integer cents.

Every amount in BusyPanel is stored as an integer number of cents: floats cannot
represent 0.1 exactly, and a business panel that is off by a cent on an invoice is
worse than useless. Parsing happens once, at the form boundary, and formatting
happens only in the UI.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Anything that is not a digit, a dot or a minus sign is noise: currency symbols,
# thousands separators, stray spaces from a copy-paste.
_NOISE = re.compile(r"[^0-9.\-]")


def parse_cents(text: str) -> int:
    """Parse a human-typed amount into integer cents.

    '150' -> 15000, '150.5' -> 15050, '$1,200.50' -> 120050, '-20' -> -2000.

    Raises ValueError on unparseable input, or on more than two decimal places
    (silently rounding money the user typed is worse than refusing it).
    """
    cleaned = _NOISE.sub("", str(text or "").strip())
    if not cleaned:
        raise ValueError("empty amount")
    # A single leading minus, and only there. "1-2" and "--5" are not amounts.
    negative = cleaned.startswith("-")
    body = cleaned[1:] if negative else cleaned
    if not body or "-" in body or "+" in body:
        raise ValueError(f"not an amount: {text!r}")
    if body.count(".") > 1:
        raise ValueError(f"not an amount: {text!r}")
    whole, _, frac = body.partition(".")
    if not whole and not frac:
        raise ValueError(f"not an amount: {text!r}")
    if len(frac) > 2:
        raise ValueError("at most two decimal places")
    try:
        value = Decimal(f"{whole or '0'}.{frac or '0'}")
    except InvalidOperation as e:
        raise ValueError(f"not an amount: {text!r}") from e
    cents = int(value.scaleb(2).to_integral_value(rounding=ROUND_HALF_UP))
    return -cents if negative else cents


def fmt_cents(cents: int) -> str:
    """120050 -> '$1,200.50'; 0 -> '$0.00'; -2000 -> '-$20.00'."""
    n = int(cents)
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n) / 100:,.2f}"
