from __future__ import annotations

from decimal import Decimal

from ar_pipeline.normalize.normalizer import NormalizerOutput
from ar_pipeline.normalize.stub_client import StubLLMClient


def _parse(user: str) -> NormalizerOutput:
    return StubLLMClient().parse(system="ignored", user=user, output_model=NormalizerOutput)


def test_returns_one_remittance_draft():
    out = _parse("From: a@b.com\nSubject: advice\n\nsome text")
    assert isinstance(out, NormalizerOutput)
    assert out.is_remittance is True
    assert "stub" in out.notes.lower()
    assert len(out.payments) == 1
    p = out.payments[0]
    assert 0.0 < p.confidence <= 0.95
    assert len(p.line_items) == 1  # always >=1 so RemittancePayload construction succeeds


def test_confidence_reflects_how_much_was_found():
    bare = _parse("Subject: hi\n\nplease see attached")
    rich = _parse("NEFT ref SBIN225551234567 — INV-2026-9 — total 1,23,456.78")
    assert bare.payments[0].confidence < 0.5
    assert rich.payments[0].confidence >= 0.9


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


def test_invoice_number_falls_back_to_labeled_table_header_code():
    out = _parse("Invoice Number\n1 | 2-Feb-26 | FCI2510007033 | 39.702")
    assert out.payments[0].line_items[0].invoice_number == "FCI2510007033"


def test_invoice_number_falls_back_to_bill_no_label():
    out = _parse("Bill No: ACM2510006275\ndated 3-Feb-26")
    assert out.payments[0].line_items[0].invoice_number == "ACM2510006275"


def test_no_amounts_still_produces_a_reviewable_draft():
    out = _parse("Subject: FW: remittance\n\nplease find attached")
    p = out.payments[0]
    assert p.total_paid_amount == Decimal("0")
    assert p.line_items[0].invoice_number == ""
    assert p.payment_reference is None


def test_ignores_small_numbers_and_years():
    out = _parse("2026 payment run, 3 invoices, total 45,000.00")
    assert out.payments[0].total_paid_amount == Decimal("45000.00")


def test_plain_integer_amount_used_only_as_a_fallback():
    # no grouped/decimal amount anywhere -> the bare integer is picked up
    out = _parse("Payment of 500000 made today (ref 2026)")
    assert out.payments[0].total_paid_amount == Decimal("500000")
    # ... but a grouped amount wins and the bare integer is ignored
    out2 = _parse("total 12,34,567.00 against PO 8899001")
    assert out2.payments[0].total_paid_amount == Decimal("1234567.00")


def test_payer_name_found_from_a_beneficiary_label():
    out = _parse("Beneficiary's name: ACME METALS LIMITED\nBeneficiary's bank: SOME BANK")
    assert out.payments[0].payer_name == "ACME METALS LIMITED"


def test_payer_name_found_from_a_vendor_name_label():
    out = _parse("Vendor Code : 220417 Vendor Name : ACME METALS LTD\nDocument No : 150000")
    assert out.payments[0].payer_name == "ACME METALS LTD"


def test_payer_name_empty_when_no_label_present():
    out = _parse("Subject: hi\n\nplease see attached, no name label here")
    assert out.payments[0].payer_name == ""


def test_currency_detected_from_an_explicit_code():
    out = _parse("We have wired USD 45,000.00 via SWIFT against Invoice EXP-2026-0456.")
    assert out.payments[0].currency == "USD"


def test_currency_defaults_to_inr_when_no_foreign_code_present():
    out = _parse("We have remitted Rs. 45,000.00 against Invoice INV-1.")
    assert out.payments[0].currency == "INR"


def test_reminder_language_with_no_payment_signal_is_not_a_remittance():
    out = _parse(
        "This is a gentle reminder that Invoice INV-2026-3381 for INR 3,42,500.00 "
        "remains unpaid. Kindly process the payment at the earliest."
    )
    assert out.is_remittance is False
    assert out.payments == []
    assert "not-a-remittance" in out.notes.lower()


def test_generic_polite_closing_does_not_misfire_as_a_reminder():
    # "kindly process" / "please arrange" are common sign-offs in genuine
    # remittance emails too (e.g. a cheque payment with no UTR to match on) —
    # only a phrase that names the payment explicitly should flip this.
    out = _parse(
        "Please find enclosed our cheque payment of INR 12,500.00 against "
        "Invoice INV-2026-501. Kindly process and please arrange an "
        "acknowledgement at your convenience."
    )
    assert out.is_remittance is True
    assert len(out.payments) == 1


def test_reminder_language_with_a_bank_reference_still_counts_as_remitted():
    # a reference or "payment done"-style phrase always wins over reminder wording
    out = _parse(
        "Kindly note this is overdue no more — we have remitted vide UTR STBK52026050100112233."
    )
    assert out.is_remittance is True
    assert len(out.payments) == 1
