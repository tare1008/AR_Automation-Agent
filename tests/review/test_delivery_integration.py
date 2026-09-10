"""Fixture -> classify/extract/normalize -> review approve -> deliver to the stub backend."""

from __future__ import annotations

from decimal import Decimal
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ar_pipeline.db.models import Delivery, Extraction
from ar_pipeline.deliver.backend_client import BackendClient
from ar_pipeline.deliver.deliverer import run_deliveries
from ar_pipeline.normalize.normalizer import NormalizerOutput, PaymentDraft
from ar_pipeline.pipeline.advance import advance_once
from ar_pipeline.schema.canonical import LineItem
from ar_pipeline.storage import LocalBlobStore
from stub_backend.app import app as stub_app
from stub_backend.store import _IDEMPOTENCY, RECEIVED
from tests.extract.vision_fake import FakeVisionExtractor
from tests.fixtures.loader import load_email
from tests.normalize.llm_fake import FakeLLMClient


def _output() -> NormalizerOutput:
    return NormalizerOutput(
        is_remittance=True,
        payments=[
            PaymentDraft(
                payer_name="Payer",
                total_paid_amount=Decimal("100.00"),
                line_items=[
                    LineItem(
                        invoice_number="INV-1",
                        invoice_amount=Decimal("100.00"),
                        amount_paid=Decimal("100.00"),
                    ),
                ],
                confidence=0.6,
            )
        ],
    )


@pytest.fixture(autouse=True)
def _clear_stub():
    RECEIVED.clear()
    _IDEMPOTENCY.clear()
    yield
    RECEIVED.clear()
    _IDEMPOTENCY.clear()


def test_pipeline_to_delivered(client, db_session, tmp_path):
    store = LocalBlobStore(str(tmp_path))
    email = load_email("02_fwd_body_table", db_session, store)
    vision = FakeVisionExtractor()
    for _ in range(3):
        advance_once(db_session, store, vision, FakeLLMClient(response=_output()))

    ext = db_session.scalars(select(Extraction).where(Extraction.email_id == email.id)).one()

    # approve through the UI (client fixture: get_db override + logged in)
    r = client.post(
        f"/review/{ext.id}/edit",
        data={
            "header.payer_name": "Payer Edited",
            "header.currency": "INR",
            "header.total_paid_amount": "100.00",
            "line_items[0].invoice_number": "INV-1",
            "line_items[0].invoice_amount": "100.00",
            "line_items[0].amount_paid": "100.00",
            "approve": "1",
        },
    )
    assert r.status_code == 303

    delivery = db_session.scalars(select(Delivery).where(Delivery.extraction_id == ext.id)).one()
    assert delivery.status == "pending"

    # TestClient is a sync httpx.Client that drives the ASGI stub in-process.
    backend = BackendClient(
        base_url="http://stub",
        http=cast(httpx.Client, TestClient(stub_app, base_url="http://stub")),
    )
    stats = run_deliveries(db_session, backend)

    assert stats.delivered == 1
    db_session.refresh(delivery)
    assert delivery.status == "delivered" and delivery.delivered_at is not None
    assert str(ext.id) in RECEIVED
    # the reviewer's edit propagated all the way to the delivered payload
    assert RECEIVED[str(ext.id)]["header"]["payer_name"] == "Payer Edited"
