from decimal import Decimal

import pytest

from ar_pipeline.db.models import Invoice
from ar_pipeline.ledger.matching import is_near_match, near_matches, number_key, payers_differ


@pytest.mark.parametrize(
    "raw", ["INV-2026-0412", "INV/2026/0412", "inv 2026 0412", "INV.2026_0412", " inv-2026-0412 "]
)
def test_number_key_ignores_case_spaces_and_separators(raw):
    assert number_key(raw) == "INV20260412"


def test_number_key_of_blank_is_empty():
    assert number_key("   ") == ""


@pytest.mark.parametrize(
    ("line", "invoice"),
    [
        ("MST-2026-07550", "MST-2026-7550"),  # rule 1: leading zeros per number group
        ("7550", "MST-2026-7550"),  # rule 2: bare suffix, >= 4 chars
        ("MST/2026/780", "MST-2026-7801"),  # rule 3: one deletion
        ("MST-2026-755O", "MST-2026-7550"),  # rule 3: one substitution (letter O)
    ],
)
def test_near_match_rules(line, invoice):
    assert is_near_match(line, invoice)


@pytest.mark.parametrize(
    ("line", "invoice"),
    [
        ("MST-2026-7550", "MST-2026-7550"),  # exact is not "near"
        ("550", "MST-2026-7550"),  # suffix shorter than 4
        ("MST-2026-7505", "MST-2026-7550"),  # two edits
        ("AB12", "AB13"),  # one edit but shorter than 6
        ("", "MST-2026-7550"),
    ],
)
def test_not_near_match(line, invoice):
    assert not is_near_match(line, invoice)


def _inv(number: str, payer: str | None) -> Invoice:
    return Invoice(
        invoice_number=number,
        number_key=number_key(number),
        payer_name=payer,
        amount=Decimal("1"),
        source="books",
    )


def test_near_matches_puts_same_payer_first_and_caps_at_three():
    invoices = [
        _inv("A-1-7550", "Other Co"),
        _inv("B-2-7550", "Meridian Steel Pvt Ltd"),
        _inv("C-3-7550", None),
        _inv("D-4-7550", "Other Co"),
        _inv("X-9-9999", "Meridian Steel"),
    ]
    found = near_matches("7550", invoices, "Meridian Steel")
    assert [i.invoice_number for i in found] == ["B-2-7550", "A-1-7550", "C-3-7550"]


@pytest.mark.parametrize(
    ("a", "b", "differ"),
    [
        ("Meridian Steel", "Meridian Steel Pvt Ltd", False),
        ("MERIDIAN STEEL PRIVATE LIMITED", "meridian steel", False),
        ("Meridian Steel Traders", "Meridian Steel", False),  # one contains the other
        ("Arcadia Foods", "Meridian Steel", True),
        (None, "Meridian Steel", False),  # only compared when both present
        ("", "Meridian Steel", False),
        ("The Co", "Meridian Steel", False),  # nothing left after stop words
    ],
)
def test_payers_differ(a, b, differ):
    assert payers_differ(a, b) is differ
