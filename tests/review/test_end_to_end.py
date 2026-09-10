"""One real fixture: ingest -> classify -> extract -> normalize -> review UI
-> edit -> approve -> a delivery row exists."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from ar_pipeline.db.models import Delivery, Extraction, ExtractionEdit
from ar_pipeline.normalize.normalizer import NormalizerOutput, PaymentDraft
from ar_pipeline.pipeline.advance import advance_once
from ar_pipeline.schema.canonical import LineItem
from ar_pipeline.storage import LocalBlobStore
from tests.extract.vision_fake import FakeVisionExtractor
from tests.fixtures.loader import load_email
from tests.normalize.llm_fake import FakeLLMClient


def _output() -> NormalizerOutput:
    return NormalizerOutput(
        is_remittance=True,
        payments=[
            PaymentDraft(
                payer_name="Fixture Payer",
                total_paid_amount=Decimal("100.00"),
                line_items=[
                    LineItem(
                        invoice_number="INV-1",
                        invoice_amount=Decimal("100.00"),
                        amount_paid=Decimal("100.00"),
                    )
                ],
                confidence=0.6,
            )
        ],
    )


def test_fixture_flows_through_review_to_delivery(client, db_session, tmp_path):
    store = LocalBlobStore(str(tmp_path))
    email = load_email("02_fwd_body_table", db_session, store)
    vision = FakeVisionExtractor()
    for _ in range(3):
        advance_once(db_session, store, vision, FakeLLMClient(response=_output()))

    ext = db_session.scalars(select(Extraction).where(Extraction.email_id == email.id)).one()
    assert ext.status == "pending_review"

    assert email.subject in client.get("/review").text

    detail = client.get(f"/review/{ext.id}")
    assert detail.status_code == 200
    assert 'name="header.payer_name"' in detail.text

    r = client.post(
        f"/review/{ext.id}/edit",
        data={
            "header.payer_name": "Fixture Payer Ltd",
            "header.currency": "INR",
            "header.total_paid_amount": "100.00",
            "line_items[0].invoice_number": "INV-1",
            "line_items[0].invoice_amount": "100.00",
            "line_items[0].amount_paid": "100.00",
            "approve": "1",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/review?flash=Approved"

    db_session.expire_all()
    ext = db_session.get(Extraction, ext.id)
    assert ext.status == "approved"
    assert ext.reviewed_by == "Asha"
    assert ext.canonical["envelope"]["reviewed_by"] == "Asha"
    assert db_session.scalars(select(Delivery).where(Delivery.extraction_id == ext.id)).all()
    assert db_session.scalars(
        select(ExtractionEdit).where(ExtractionEdit.extraction_id == ext.id)
    ).all()
