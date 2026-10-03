from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ar_pipeline.db.models import Email, Extraction, Invoice, InvoicePayment
from ar_pipeline.ledger.matching import number_key


def canonical_for(
    *,
    invoice_number: str = "INV-1",
    invoice_amount: str = "100.00",
    amount_paid: str = "100.00",
    tds: str | None = None,
    payer_name: str = "Acme Corp",
    currency: str = "INR",
    reference: str | None = "UTR-1",
) -> dict:
    deductions = [{"type": "tds", "amount": tds, "reason": None}] if tds else []
    total = Decimal(amount_paid)
    return {
        "envelope": {
            "extraction_id": str(uuid.uuid4()),
            "source_email_id": "email-x",
            "payment_index": 0,
            "vendor_guess": None,
            "extracted_at": datetime(2026, 9, 9, tzinfo=UTC).isoformat(),
            "reviewed_by": None,
        },
        "header": {
            "payer_name": payer_name,
            "payer_id": None,
            "payment_reference": reference,
            "payment_reference_type": "utr",
            "payment_date": "2026-09-20",
            "payment_method": "NEFT",
            "currency": currency,
            "total_paid_amount": str(total),
            "deductions": [],
        },
        "line_items": [
            {
                "invoice_number": invoice_number,
                "invoice_date": None,
                "invoice_amount": invoice_amount,
                "deductions": deductions,
                "amount_paid": amount_paid,
            }
        ],
    }


@pytest.fixture
def make_invoice(db_session):
    def _make(
        number: str = "INV-1",
        amount: str = "100.00",
        *,
        source: str = "books",
        payer: str | None = "Acme Corp",
        currency: str = "INR",
        paid_before_import: str = "0",
    ) -> Invoice:
        inv = Invoice(
            invoice_number=number,
            number_key=number_key(number),
            payer_name=payer,
            amount=Decimal(amount),
            currency=currency,
            source=source,
            paid_before_import=Decimal(paid_before_import),
        )
        db_session.add(inv)
        db_session.flush()
        return inv

    return _make


@pytest.fixture
def make_extraction(db_session):
    def _make(status: str = "pending_review", subject: str = "payment", **canon) -> Extraction:
        email = Email(
            internet_message_id=f"m-{uuid.uuid4()}",
            sender_address="ap@payer.example",
            sender_domain="payer.example",
            subject=subject,
            received_at=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
            status="review",
        )
        db_session.add(email)
        db_session.flush()
        ext = Extraction(
            email_id=email.id,
            canonical=canonical_for(**canon),
            confidence=Decimal("0.95"),
            is_remittance=True,
            validation_flags=[],
            status=status,
        )
        db_session.add(ext)
        db_session.flush()
        return ext

    return _make


@pytest.fixture
def post_payment(db_session, make_extraction):
    """Record an approved payment of ``settled`` directly (bypasses posting)."""

    def _post(
        invoice: Invoice,
        settled: str,
        *,
        currency: str = "INR",
        reference: str = "UTR-9",
        payment_date: date | None = None,
    ):
        ext = make_extraction(status="approved")
        row = InvoicePayment(
            invoice_id=invoice.id,
            extraction_id=ext.id,
            line_index=0,
            amount_paid=Decimal(settled),
            deductions_total=Decimal("0"),
            settled=Decimal(settled),
            currency=currency,
            payment_reference=reference,
            payment_date=payment_date,
        )
        db_session.add(row)
        db_session.flush()
        return row

    return _post


@pytest.fixture
def seed_extraction(db_session):
    """An email + an approved remittance extraction holding `canonical`."""

    def _make(canonical: dict) -> Extraction:
        email = Email(
            internet_message_id=f"m-{uuid.uuid4()}",
            sender_address="a@b.com",
            sender_domain="b.com",
            subject="advice",
            received_at=datetime(2026, 10, 1, tzinfo=UTC),
            status="done",
        )
        db_session.add(email)
        db_session.flush()
        ext = Extraction(
            email_id=email.id, canonical=canonical, is_remittance=True, status="approved"
        )
        db_session.add(ext)
        db_session.flush()
        return ext

    return _make
