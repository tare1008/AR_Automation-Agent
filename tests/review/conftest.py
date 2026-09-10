from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ar_pipeline.db.models import Email, Extraction


def _canonical(payment_index: int = 0) -> dict:
    return {
        "envelope": {
            "extraction_id": str(uuid.uuid4()),
            "source_email_id": "email-x",
            "payment_index": payment_index,
            "vendor_guess": "Acme",
            "extracted_at": datetime(2026, 9, 9, tzinfo=UTC).isoformat(),
            "reviewed_by": None,
        },
        "header": {
            "payer_name": "Acme Corp",
            "payer_id": None,
            "payment_reference": "UTR-1",
            "payment_reference_type": "utr",
            "payment_date": "2026-09-05",
            "payment_method": "RTGS",
            "currency": "INR",
            "total_paid_amount": "90.00",
            "deductions": [],
        },
        "line_items": [
            {
                "invoice_number": "INV-1",
                "invoice_date": "2026-08-01",
                "invoice_amount": "100.00",
                "deductions": [{"type": "tds", "amount": "10.00", "reason": "194Q"}],
                "amount_paid": "90.00",
            }
        ],
    }


@pytest.fixture
def seed_pending(db_session):
    """Insert one email in `review` + one pending-review extraction; return (email, extraction)."""

    def _make(
        *, is_remittance: bool = True, canonical: dict | None = None
    ) -> tuple[Email, Extraction]:
        email = Email(
            internet_message_id=f"m-{uuid.uuid4()}",
            sender_address="staff@ourco.com",
            sender_domain="ourco.com",
            subject="FW: payment advice",
            received_at=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
            body_html="<p>advice attached</p>",
            body_text="advice attached",
            status="review",
        )
        db_session.add(email)
        db_session.flush()
        ext = Extraction(
            email_id=email.id,
            canonical=canonical if canonical is not None else _canonical(),
            confidence=Decimal("0.7"),
            is_remittance=is_remittance,
            validation_flags=[],
            llm_model="claude-opus-5",
            prompt_version="2",
            status="pending_review",
        )
        db_session.add(ext)
        db_session.flush()
        return email, ext

    return _make
