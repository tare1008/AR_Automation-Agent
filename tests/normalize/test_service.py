from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from ar_pipeline.db.models import Email, Extraction, ExtractionSource, RawExtraction
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
    # every row's envelope carries its own authoritative PK, not a placeholder uuid
    assert all(r.canonical["envelope"]["extraction_id"] == str(r.id) for r in rows)


def test_poison_llm_refusal_isolated_from_healthy_sibling(db_session, store):
    a = load_email("05_direct_body_freetext", db_session, store)
    b = load_email("06_direct_excel", db_session, store)

    advance_once(db_session, store, FakeVisionExtractor(), FakeLLMClient())  # classify both
    advance_once(db_session, store, FakeVisionExtractor(), FakeLLMClient())  # extract both
    for email in (a, b):
        db_session.refresh(email)
        assert email.status == "extracted"

    # first email to be normalized refuses; its sibling gets a good payload.
    client = FakeLLMClient(
        responses=[
            LLMRefused("nope"),
            NormalizerOutput(is_remittance=True, payments=[_reconciling_draft()]),
        ]
    )
    stats = advance_once(db_session, store, FakeVisionExtractor(), client)

    assert stats.errored == 1
    assert stats.normalized == 1

    for email in (a, b):
        db_session.refresh(email)
    refused = a if a.status == "error" else b
    healthy = b if refused is a else a

    assert refused.status == "error"
    assert refused.error_detail and "LLMRefused" in refused.error_detail
    assert healthy.status == "review"

    refused_rows = db_session.scalars(
        select(Extraction).where(Extraction.email_id == refused.id)
    ).all()
    healthy_rows = db_session.scalars(
        select(Extraction).where(Extraction.email_id == healthy.id)
    ).all()
    assert refused_rows == []  # savepoint rollback left no Extraction rows
    assert len(healthy_rows) == 1

    # session still usable
    assert len(db_session.scalars(select(Email)).all()) == 2


def test_normalize_one_deterministic_source_order(db_session, store):
    email = _to_extracted("05_direct_body_freetext", db_session, store)

    # hand-insert two sources whose kinds sort the opposite way to insertion
    # order (pdf_text before excel), plus a distinct RawExtraction each.
    s1 = ExtractionSource(email_id=email.id, kind="pdf_text", ref="att-1")
    s2 = ExtractionSource(email_id=email.id, kind="excel", ref="att-2")
    db_session.add_all([s1, s2])
    db_session.flush()
    for src in (s1, s2):
        db_session.add(
            RawExtraction(
                extraction_source_id=src.id,
                payload={"text": f"TEXT-{src.kind}", "tables": [], "meta": {}},
                extractor_version="1",
            )
        )
    db_session.flush()

    client = FakeLLMClient(
        responses=[
            NormalizerOutput(is_remittance=True, payments=[_reconciling_draft()]),
            NormalizerOutput(is_remittance=True, payments=[_reconciling_draft()]),
        ]
    )
    normalize_one(db_session, email, client)
    email.status = "extracted"
    db_session.flush()
    normalize_one(db_session, email, client)

    def order(msg: str) -> list[str]:
        return [ln for ln in msg.splitlines() if ln.startswith("TEXT-")]

    first, second = order(client.calls[0]["user"]), order(client.calls[1]["user"])
    assert first == second  # deterministic across runs
    # kind-sorted: "TEXT-excel" precedes "TEXT-pdf_text" despite reverse insertion.
    assert first == ["TEXT-excel", "TEXT-pdf_text"]


def test_normalize_one_no_raw_extractions_errors(db_session, store):
    email = load_email("05_direct_body_freetext", db_session, store)
    email.status = "extracted"
    db_session.flush()

    count = normalize_one(db_session, email, FakeLLMClient())

    assert count == 0
    db_session.refresh(email)
    assert email.status == "error"
    assert email.error_detail == "no raw extractions to normalize"
    rows = db_session.scalars(select(Extraction).where(Extraction.email_id == email.id)).all()
    assert rows == []
