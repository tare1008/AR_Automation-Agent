from __future__ import annotations

from decimal import Decimal

import pytest

from ar_pipeline.tables.words import amount_in_words


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "for rupees ( ONE CRORE THIRTY ONE LAKH THIRTEEN THOUSAND THREE HUNDRED TWENTY "
            "FIVE RupeesTHIRTY NINE Paise ) as detailed below.",
            Decimal("13113325.39"),
        ),
        ("Rupees One Lakh Only", Decimal("100000.00")),
        ("Rs. Five Thousand and Fifty Paise only", Decimal("5000.50")),
        ("INR: Rupees Twelve Lacs Fifty Thousand only", Decimal("1250000.00")),
        ("Rupees Nine Hundred Ninety Nine only", Decimal("999.00")),
        ("one invoice and two credit notes", None),
        ("no amount here at all", None),
    ],
)
def test_amount_in_words(text, expected):
    assert amount_in_words(text) == expected
