from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from ar_pipeline.db.models import Email, Extraction
from ar_pipeline.normalize.llm_client import LLMRefused
from ar_pipeline.normalize.normalizer import NormalizerOutput, PaymentDraft
from ar_pipeline.normalize.service import normalize_one
from ar_pipeline.pipeline.advance import advance_once
from ar_pipeline.schema.canonical import Deduction, LineItem
from ar_pipeline.storage import LocalBlobStore
from tests.extract.vision_fake import FakeVisionExtractor
from tests.fixtures.loader import load_email
from tests.normalize.llm_fake import FakeLLMClient


@pytest.fixture
def store(tmp_path):
    return LocalBlobStore(str(tmp_path))


def _reconciling_draft(payment_reference: str | None = None) -> PaymentDraft:
    return PaymentDraft(
        payer_name="Acme Corp",
        payment_reference=payment_reference,
        total_paid_amount=Decimal("90.00"),
        line_items=[
            LineItem(
                invoice_number="INV-1",
                invoice_amount=Decimal("100.00"),
                deductions=[Deduction(type="tds", amount=Decimal("10.00"))],
                amount_paid=Decimal("90.00"),
            )
        ],
        confidence=0.9,
    )


def _to_extracted(name: str, session, store) -> Email:
    email = load_email(name, session, store)
    advance_once(session, store, FakeVisionExtractor(), FakeLLMClient())
    advance_once(session, store, FakeVisionExtractor(), FakeLLMClient())
    session.refresh(email)
    assert email.status == "extracted"
    return email


def test_normalize_one_single_payment(db_session, store):
    email = _to_extracted("05_direct_body_freetext", db_session, store)
    client = FakeLLMClient(
        response=NormalizerOutput(is_remittance=True, payments=[_reconciling_draft()])
    )

    count = normalize_one(db_session, email, client)

    assert count == 1
    db_session.refresh(email)
    assert email.status == "review"
    rows = db_session.scalars(select(Extraction).where(Extraction.email_id == email.id)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "pending_review"
    assert row.canonical["header"]["total_paid_amount"] is not None
    assert row.llm_model == "claude-opus-5"
    assert row.prompt_version == "2"


def test_normalize_one_not_a_remittance(db_session, store):
    email = _to_extracted("05_direct_body_freetext", db_session, store)
    client = FakeLLMClient(
        response=NormalizerOutput(is_remittance=False, notes="newsletter", payments=[])
    )

    count = normalize_one(db_session, email, client)

    assert count == 1
    db_session.refresh(email)
    assert email.status == "review"
    rows = db_session.scalars(select(Extraction).where(Extraction.email_id == email.id)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.is_remittance is False
    assert row.canonical == {}
    assert row.validation_flags == ["LLM: not a remittance"]


def test_normalize_one_two_payments(db_session, store):
    email = _to_extracted("05_direct_body_freetext", db_session, store)
    client = FakeLLMClient(
        response=NormalizerOutput(
            is_remittance=True,
            payments=[_reconciling_draft("UTR111"), _reconciling_draft("UTR222")],
        )
    )

    count = normalize_one(db_session, email, client)

    assert count == 2
    rows = db_session.scalars(select(Extraction).where(Extraction.email_id == email.id)).all()
    assert len(rows) == 2
    assert {r.canonical["envelope"]["payment_index"] for r in rows} == {0, 1}


def test_poison_llm_refusal_via_advance_once(db_session, store):
    a = load_email("05_direct_body_freetext", db_session, store)
    b = load_email("06_direct_excel", db_session, store)

    advance_once(db_session, store, FakeVisionExtractor(), FakeLLMClient())  # classify both
    advance_once(db_session, store, FakeVisionExtractor(), FakeLLMClient())  # extract both
    for email in (a, b):
        db_session.refresh(email)
        assert email.status == "extracted"

    stats = advance_once(
        db_session, store, FakeVisionExtractor(), FakeLLMClient(error=LLMRefused("nope"))
    )

    assert stats.errored == 2
    assert stats.normalized == 0
    for email in (a, b):
        db_session.refresh(email)
        assert email.status == "error"
        assert email.error_detail
        assert "LLMRefused" in email.error_detail

    # session still usable
    assert len(db_session.scalars(select(Email)).all()) == 2
