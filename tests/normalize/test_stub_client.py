from __future__ import annotations

from decimal import Decimal

from ar_pipeline.normalize.normalizer import NormalizerOutput
from ar_pipeline.normalize.stub_client import StubLLMClient


def _parse(user: str) -> NormalizerOutput:
    return StubLLMClient().parse(system="ignored", user=user, output_model=NormalizerOutput)


def test_returns_one_low_confidence_remittance():
    out = _parse("From: a@b.com\nSubject: advice\n\nsome text")
    assert isinstance(out, NormalizerOutput)
    assert out.is_remittance is True
    assert "stub" in out.notes.lower()
    assert len(out.payments) == 1
    p = out.payments[0]
    assert p.confidence == 0.15
    assert len(p.line_items) == 1  # always >=1 so RemittancePayload construction succeeds


def test_pulls_the_largest_amount_as_total():
    out = _parse("NEFT payment. Invoice INV-42 for 1,23,456.78 net of TDS 123.46 = 1,23,333.32")
    p = out.payments[0]
    assert p.total_paid_amount == Decimal("123456.78")
    assert p.line_items[0].invoice_amount == Decimal("123456.78")
    assert p.line_items[0].amount_paid == Decimal("123456.78")


def test_extracts_a_bank_reference_and_invoice_token():
    out = _parse("Payment advice\nUTR: SBIN225551234567\nAgainst INV/2026/0091")
    p = out.payments[0]
    assert p.payment_reference == "SBIN225551234567"
    assert p.payment_reference_type == "utr"
    assert p.line_items[0].invoice_number == "2026/0091"


def test_does_not_mistake_the_word_invoice_for_an_id():
    for text in ("3 invoices attached", "Invoice Date: 01-02-2026", "please see invoice"):
        assert _parse(f"total 5,000.00\n{text}").payments[0].line_items[0].invoice_number == ""


def test_no_amounts_still_produces_a_reviewable_draft():
    out = _parse("Subject: FW: remittance\n\nplease find attached")
    p = out.payments[0]
    assert p.total_paid_amount == Decimal("0")
    assert p.line_items[0].invoice_number == ""
    assert p.payment_reference is None


def test_ignores_small_numbers_and_years():
    out = _parse("2026 payment run, 3 invoices, total 45,000.00")
    assert out.payments[0].total_paid_amount == Decimal("45000.00")
