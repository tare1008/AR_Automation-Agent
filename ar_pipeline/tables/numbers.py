"""Amounts and dates as payment advices print them. Pure — no DB, no I/O."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_CURRENCY_RE = re.compile(r"(?i)rs\.?|inr|₹")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_DATE_FORMATS = (
    "%d.%m.%Y",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%y",
    "%d/%m/%y",
    "%d-%m-%y",
    "%Y-%m-%d",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d-%b-%y",
    "%d %B %Y",
)


def parse_amount(raw: str | None) -> Decimal | None:
    """'2,377.00-' -> -2377.00, '(1,234.50)' -> -1234.50, '1,23,456.78' -> 123456.78;
    None for blanks and anything that is not a plain amount (dates, codes)."""
    if raw is None:
        return None
    s = _CURRENCY_RE.sub("", str(raw)).strip().replace(" ", "")
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s.endswith("/-"):
        s = s[:-2]
    elif s.endswith("/"):
        s = s[:-1]
    if s.endswith("-"):
        negative, s = True, s[:-1]
    if s.startswith("-"):
        negative, s = True, s[1:]
    # Reject empty comma groups
    if s.startswith(",") or s.endswith(",") or ",," in s:
        return None
    s = s.replace(",", "")
    if not _NUMBER_RE.fullmatch(s):
        return None
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return -value if negative else value


def parse_date(raw: str | None) -> date | None:
    """Day-first, as Indian advices print dates."""
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None
