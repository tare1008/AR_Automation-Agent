import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

import ar_pipeline.config as config_module
from ar_pipeline.db.models import Email, Extraction, ExtractionSource, RawExtraction
from ar_pipeline.normalize.normalizer import NormalizerOutput, PaymentDraft
from ar_pipeline.normalize.service import normalize_one
from ar_pipeline.review.auth import User
from ar_pipeline.review.service import ApprovalBlocked, save_edits
from ar_pipeline.schema.canonical import LineItem
from tests.normalize.llm_fake import FakeLLMClient

ASHA = User(name="Asha")


@pytest.fixture
def auto_approve(monkeypatch):
    monkeypatch.setenv("AUTO_APPROVE_MIN_CONFIDENCE", "0.75")
    config_module.get_settings.cache_clear()
    yield
    config_module.get_settings.cache_clear()


def _extracted_email(db_session, mid: str) -> Email:
    email = Email(
        internet_message_id=mid,
        sender_address="a@b.com",
        sender_domain="b.com",
        subject=mid,
        received_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
        status="extracted",
    )
    db_session.add(email)
    db_session.flush()
    src = ExtractionSource(email_id=email.id, kind="body_text", ref="body")
    db_session.add(src)
    db_session.flush()
    db_session.add(RawExtraction(extraction_source_id=src.id, payload={"text": "x", "tables": []}))
    db_session.flush()
    return email


def _output(*paid: str) -> NormalizerOutput:
    return NormalizerOutput(
        is_remittance=True,
        payments=[
            PaymentDraft(
                payer_name="Acme Corp",
                payment_reference=f"UTR-{p}",
                total_paid_amount=Decimal(p),
                line_items=[
                    LineItem(
                        invoice_number="INV-1", invoice_amount=Decimal(p), amount_paid=Decimal(p)
                    )
                ],
                confidence=0.95,
            )
            for p in paid
        ],
    )


def _rows(db_session, email):
    return db_session.scalars(
        select(Extraction).where(Extraction.email_id == email.id).order_by(Extraction.id)
    ).all()


def test_installment_on_books_invoice_auto_approves(db_session, make_invoice, auto_approve):
    make_invoice("INV-1", "100")
    email = _extracted_email(db_session, "m-inst-1")
    normalize_one(db_session, email, FakeLLMClient(response=_output("25")))
    (row,) = _rows(db_session, email)
    assert row.status == "approved"
    assert row.validation_flags == []


def test_second_payment_in_the_same_email_sees_the_first(db_session, make_invoice, auto_approve):
    make_invoice("INV-1", "100")
    email = _extracted_email(db_session, "m-race-auto")
    normalize_one(db_session, email, FakeLLMClient(response=_output("60", "50")))
    statuses = sorted((r.status, tuple(r.validation_flags)) for r in _rows(db_session, email))
    assert statuses[0][0] == "approved"
    assert statuses[1][0] == "pending_review"
    assert any("overpaid by ₹10.00" in f for f in statuses[1][1])


def test_reviewer_approval_is_stopped_when_checks_changed(
    db_session, make_invoice, make_extraction
):
    make_invoice("INV-1", "100")
    a = make_extraction(invoice_amount="60", amount_paid="60", reference="UTR-A")
    b = make_extraction(invoice_amount="50", amount_paid="50", reference="UTR-B")
    form = lambda ext: {  # noqa: E731
        "header": ext.canonical["header"],
        "line_items": ext.canonical["line_items"],
    }

    save_edits(db_session, a.id, ASHA, form(a), approve=True)
    assert a.status == "approved"

    with pytest.raises(ApprovalBlocked):
        save_edits(db_session, b.id, ASHA, form(b), approve=True)
    db_session.refresh(b)
    assert b.status == "pending_review"
    assert any("overpaid by ₹10.00" in f for f in b.validation_flags)

    # approving again, having now seen the flag, goes through
    save_edits(db_session, b.id, ASHA, form(b), approve=True)
    assert b.status == "approved"
