from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from ar_pipeline.db.models import Attachment, Email, Extraction
from ar_pipeline.normalize.stub_client import StubLLMClient
from ar_pipeline.pipeline.advance import advance_once
from ar_pipeline.storage import LocalBlobStore
from ar_pipeline.tables.models import HeaderOutput, MappingOutput
from tests.extract.vision_fake import FakeVisionExtractor
from tests.tables.advice_pdf import NET, build_advice_pdf


class Counting:
    def __init__(self):
        self.inner = StubLLMClient()
        self.models: list[type] = []

    def parse(self, *, system, user, output_model):
        self.models.append(output_model)
        return self.inner.parse(system=system, user=user, output_model=output_model)


@pytest.fixture
def store(tmp_path):
    return LocalBlobStore(str(tmp_path))


def _advice_email(session, store, n: int) -> Email:
    data = build_advice_pdf()
    email = Email(
        internet_message_id=f"<advice-{n}@fixture>",
        sender_address="ap@ourco.com",
        sender_domain="ourco.com",
        subject="FW: Payment Advice",
        received_at=datetime(2026, 1, 20, tzinfo=UTC),
        body_html="",
        body_text="Please find attached the payment advice.",
        status="new",
    )
    session.add(email)
    session.flush()
    att = Attachment(
        email_id=email.id,
        filename="advice.pdf",
        content_type="application/pdf",
        size=len(data),
        blob_url="",
        sha256="0" * 64,
    )
    session.add(att)
    session.flush()
    att.blob_url = store.put(f"{email.id}/{att.id}/advice.pdf", data)
    session.flush()
    return email


def _run(session, store, llm) -> None:
    for _ in range(3):
        advance_once(session, store, FakeVisionExtractor(), llm)


def test_advice_end_to_end_offline(db_session, store, monkeypatch):
    from ar_pipeline.config import get_settings

    monkeypatch.setenv("CLIENT_NAMES", "Acme Metals")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    get_settings.cache_clear()
    try:
        first = _advice_email(db_session, store, 1)
        llm = Counting()
        _run(db_session, store, llm)
        db_session.refresh(first)
        assert first.status == "review"
        ext = db_session.scalar(select(Extraction).where(Extraction.email_id == first.id))
        assert len(ext.canonical["line_items"]) == 20
        assert Decimal(ext.canonical["header"]["total_paid_amount"]) == NET
        assert ext.canonical["header"]["payer_name"] == "CONTINENTAL BUS BODY BUILDERS LIMITED"
        assert ext.canonical["line_items"][1]["applies_to"] == "CBB2510004583"
        assert ext.read_info["path"] == "table"
        assert not any(f.startswith("header: doesn't match") for f in ext.validation_flags)
        assert llm.models.count(MappingOutput) == 1

        second = _advice_email(db_session, store, 2)
        llm2 = Counting()
        _run(db_session, store, llm2)
        db_session.refresh(second)
        assert MappingOutput not in llm2.models and HeaderOutput in llm2.models
    finally:
        get_settings.cache_clear()
