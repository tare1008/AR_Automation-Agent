from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ar_pipeline.tables.numbers import parse_amount, parse_date


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2,804,859.17", Decimal("2804859.17")),
        ("1,23,456.78", Decimal("123456.78")),
        ("2,377.00-", Decimal("-2377.00")),
        ("-15,924.00", Decimal("-15924.00")),
        ("(1,234.50)", Decimal("-1234.50")),
        ("Rs. 500", Decimal("500")),
        ("₹ 1,000.00", Decimal("1000.00")),
        ("0.00", Decimal("0.00")),
        ("", None),
        (None, None),
        ("27.11.2025", None),
        ("2510004583DISCO", None),
    ],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("27.11.2025", date(2025, 11, 27)),
        ("05/11/2025", date(2025, 11, 5)),
        ("05-11-25", date(2025, 11, 5)),
        ("2025-11-05", date(2025, 11, 5)),
        ("5-Nov-2025", date(2025, 11, 5)),
        ("", None),
        ("not a date", None),
    ],
)
def test_parse_date_day_first(raw, expected):
    assert parse_date(raw) == expected
