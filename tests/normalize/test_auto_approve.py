from __future__ import annotations

import datetime
from decimal import Decimal

from sqlalchemy import select

import ar_pipeline.config as config_module
from ar_pipeline.db.models import Delivery, Email, Extraction, ExtractionSource, RawExtraction
from ar_pipeline.normalize.normalizer import NormalizerOutput, PaymentDraft
from ar_pipeline.normalize.service import normalize_one
from ar_pipeline.schema.canonical import LineItem
from tests.normalize.llm_fake import FakeLLMClient


def _extracted_email(db_session, mid: str) -> Email:
    email = Email(
        internet_message_id=mid,
        sender_address="a@b.com",
        sender_domain="b.com",
        subject="s",
        received_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
        status="extracted",
    )
    db_session.add(email)
    db_session.flush()
    src = ExtractionSource(email_id=email.id, kind="body_text", ref="body")
    db_session.add(src)
    db_session.flush()
    db_session.add(
        RawExtraction(extraction_source_id=src.id, payload={"text": "advice", "tables": []})
    )
    db_session.flush()
    return email


def _clean_output(conf: float) -> NormalizerOutput:
    return NormalizerOutput(
        is_remittance=True,
        payments=[
            PaymentDraft(
                payer_name="Acme",
                total_paid_amount=Decimal("100.00"),
                line_items=[
                    LineItem(
                        invoice_number="INV-1",
                        invoice_amount=Decimal("100.00"),
                        amount_paid=Decimal("100.00"),
                    )
                ],
                confidence=conf,
            )
        ],
    )


def test_high_confidence_flag_free_extraction_is_auto_approved(db_session, monkeypatch):
    monkeypatch.setenv("AUTO_APPROVE_MIN_CONFIDENCE", "0.75")
    config_module.get_settings.cache_clear()
    try:
        email = _extracted_email(db_session, "m-auto-1")
        normalize_one(db_session, email, FakeLLMClient(response=_clean_output(0.9)))
        db_session.refresh(email)
        ext = db_session.scalars(select(Extraction).where(Extraction.email_id == email.id)).one()
        assert ext.status == "approved"
        assert ext.reviewed_by == "auto"
        assert email.status == "done"
        assert db_session.scalars(select(Delivery).where(Delivery.extraction_id == ext.id)).all()
    finally:
        config_module.get_settings.cache_clear()


def test_low_confidence_extraction_still_goes_to_review(db_session, monkeypatch):
    monkeypatch.setenv("AUTO_APPROVE_MIN_CONFIDENCE", "0.75")
    config_module.get_settings.cache_clear()
    try:
        email = _extracted_email(db_session, "m-auto-2")
        normalize_one(db_session, email, FakeLLMClient(response=_clean_output(0.3)))
        db_session.refresh(email)
        ext = db_session.scalars(select(Extraction).where(Extraction.email_id == email.id)).one()
        assert ext.status == "pending_review"
        assert email.status == "review"
    finally:
        config_module.get_settings.cache_clear()


def test_threshold_zero_keeps_everything_in_review(db_session):
    # default Settings() -> auto_approve_min_confidence == 0.0
    email = _extracted_email(db_session, "m-auto-3")
    normalize_one(db_session, email, FakeLLMClient(response=_clean_output(0.99)))
    db_session.refresh(email)
    ext = db_session.scalars(select(Extraction).where(Extraction.email_id == email.id)).one()
    assert ext.status == "pending_review"


def test_a_validation_flag_blocks_auto_approval_even_at_high_confidence(db_session, monkeypatch):
    monkeypatch.setenv("AUTO_APPROVE_MIN_CONFIDENCE", "0.75")
    config_module.get_settings.cache_clear()
    try:
        email = _extracted_email(db_session, "m-auto-flagged")
        output = _clean_output(0.99)
        output.payments[0].line_items[0].invoice_number = ""  # forces "empty invoice number"
        normalize_one(db_session, email, FakeLLMClient(response=output))
        db_session.refresh(email)
        ext = db_session.scalars(select(Extraction).where(Extraction.email_id == email.id)).one()
        assert ext.status == "pending_review"
        assert ext.validation_flags  # a real checkpoint failure, not auto-approved despite 0.99
    finally:
        config_module.get_settings.cache_clear()
