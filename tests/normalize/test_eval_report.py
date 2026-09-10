from __future__ import annotations

from decimal import Decimal

from ar_pipeline.normalize.normalizer import NormalizerOutput, PaymentDraft
from ar_pipeline.schema.canonical import LineItem
from tests.normalize import eval_report


def _draft(payer: str, ref: str | None) -> PaymentDraft:
    return PaymentDraft(
        payer_name=payer,
        payment_reference=ref,
        total_paid_amount=Decimal("90.00"),
        line_items=[
            LineItem(
                invoice_number="INV-1",
                invoice_amount=Decimal("90.00"),
                deductions=[],
                amount_paid=Decimal("90.00"),
            )
        ],
        confidence=0.87,
    )


def test_summarise_two_payments_one_flagged() -> None:
    out = NormalizerOutput(
        is_remittance=True,
        notes="two transfers, second reconciles",
        payments=[_draft("Acme Corp", "UTR111"), _draft("Globex Ltd", "UTR222")],
    )
    flags_per_payment = [["payment total 90.00 != 100.00"], []]

    text = eval_report.summarise("03_something", out, flags_per_payment)

    assert "03_something" in text
    assert "is_remittance:" in text
    assert "Acme Corp" in text
    assert "Globex Ltd" in text
    assert "payment total 90.00 != 100.00" in text
    assert "clean" in text
    assert "two transfers, second reconciles" in text


def test_summarise_not_a_remittance() -> None:
    out = NormalizerOutput(is_remittance=False, notes="", payments=[])

    text = eval_report.summarise("09_newsletter", out, [])

    assert "is_remittance: False" in text
    assert "payments: 0" in text


def test_summarise_defensive_when_flags_shorter_than_payments() -> None:
    out = NormalizerOutput(
        is_remittance=True,
        notes="",
        payments=[_draft("Acme Corp", None), _draft("Globex Ltd", None)],
    )

    text = eval_report.summarise("x", out, [["bad"]])

    assert "bad" in text
    # second payment has no entry -> treated as clean, not an IndexError
    assert text.count("clean") == 1
