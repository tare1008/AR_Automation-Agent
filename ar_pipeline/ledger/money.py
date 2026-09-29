"""Money display: INR with Indian digit grouping (₹1,00,000.00), others as
``USD 4,250.00``."""

from __future__ import annotations

from decimal import Decimal


def _indian_grouping(whole: str) -> str:
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


def format_money(amount: Decimal, currency: str = "INR") -> str:
    q = Decimal(amount).quantize(Decimal("0.01"))
    sign = "-" if q < 0 else ""
    whole, frac = f"{abs(q):.2f}".split(".")
    if currency == "INR":
        return f"{sign}₹{_indian_grouping(whole)}.{frac}"
    return f"{sign}{currency} {int(whole):,}.{frac}"
