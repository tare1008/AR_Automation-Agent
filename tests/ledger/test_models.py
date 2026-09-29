from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from ar_pipeline.db.models import Email, Extraction, Invoice, InvoicePayment


def _extraction(db_session) -> Extraction:
    email = Email(
        internet_message_id="m-ledger-model",
        sender_address="a@b.com",
        sender_domain="b.com",
        subject="s",
        received_at=datetime(2026, 9, 1, tzinfo=UTC),
        status="review",
    )
    db_session.add(email)
    db_session.flush()
    ext = Extraction(email_id=email.id, canonical={}, status="approved")
    db_session.add(ext)
    db_session.flush()
    return ext


def test_invoice_and_payment_round_trip(db_session):
    inv = Invoice(
        invoice_number="INV-1",
        number_key="INV1",
        amount=Decimal("100.00"),
        source="books",
    )
    db_session.add(inv)
    db_session.flush()
    ext = _extraction(db_session)
    pay = InvoicePayment(
        invoice_id=inv.id,
        extraction_id=ext.id,
        line_index=0,
        amount_paid=Decimal("25.00"),
        deductions_total=Decimal("0.00"),
        settled=Decimal("25.00"),
        currency="INR",
    )
    db_session.add(pay)
    db_session.flush()
    db_session.refresh(inv)
    assert inv.currency == "INR"
    assert inv.paid_before_import == Decimal("0.00")
    assert pay.payment_reference is None


def test_number_key_is_unique(db_session):
    db_session.add(
        Invoice(
            invoice_number="INV-1",
            number_key="INV1",
            amount=Decimal("1"),
            source="books",
        )
    )
    db_session.flush()
    db_session.add(
        Invoice(
            invoice_number="inv/1",
            number_key="INV1",
            amount=Decimal("1"),
            source="email",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_one_payment_row_per_extraction_line(db_session):
    inv = Invoice(
        invoice_number="INV-1",
        number_key="INV1",
        amount=Decimal("100"),
        source="books",
    )
    db_session.add(inv)
    db_session.flush()
    ext = _extraction(db_session)
    for _ in range(2):
        db_session.add(
            InvoicePayment(
                invoice_id=inv.id,
                extraction_id=ext.id,
                line_index=0,
                amount_paid=Decimal("1"),
                deductions_total=Decimal("0"),
                settled=Decimal("1"),
                currency="INR",
            )
        )
    with pytest.raises(IntegrityError):
        db_session.flush()
