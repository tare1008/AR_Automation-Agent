from decimal import Decimal

import pytest

from ar_pipeline.ledger.money import format_money


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        (Decimal("100000"), "INR", "₹1,00,000.00"),
        (Decimal("12345678.5"), "INR", "₹1,23,45,678.50"),
        (Decimal("25"), "INR", "₹25.00"),
        (Decimal("999"), "INR", "₹999.00"),
        (Decimal("-5"), "INR", "-₹5.00"),
        (Decimal("4250"), "USD", "USD 4,250.00"),
    ],
)
def test_format_money(amount, currency, expected):
    assert format_money(amount, currency) == expected
