from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from ar_pipeline.db.models import Attachment, Email, ExtractionSource, RawExtraction
from ar_pipeline.pipeline.advance import AdvanceStats, advance_once
from ar_pipeline.storage import LocalBlobStore
from tests.extract.vision_fake import FakeVisionExtractor
from tests.fixtures.loader import load_email


@pytest.fixture
def store(tmp_path):
    return LocalBlobStore(str(tmp_path))


def test_advance_once_classifies_then_extracts_body_email(db_session, store):
    email = load_email("05_bharat_body_freetext", db_session, store)

    stats = advance_once(db_session, store, FakeVisionExtractor())
    assert isinstance(stats, AdvanceStats)
    assert stats.classified == 1
    assert stats.extracted == 0
    db_session.refresh(email)
    assert email.status == "classified"
    sources = db_session.scalars(
        select(ExtractionSource).where(ExtractionSource.email_id == email.id)
    ).all()
    assert len(sources) >= 1

    stats2 = advance_once(db_session, store, FakeVisionExtractor())
    assert stats2.extracted == 1
    db_session.refresh(email)
    assert email.status == "extracted"

    raws = db_session.scalars(
        select(RawExtraction)
        .join(ExtractionSource, RawExtraction.extraction_source_id == ExtractionSource.id)
        .where(ExtractionSource.email_id == email.id)
    ).all()
    assert raws
    assert any(r.payload.get("text") for r in raws)


def test_email_with_only_skipped_sources_errors(db_session, store):
    # 67-byte inline PNG named like a logo -> classifier skips it, and the body
    # is empty, so classification yields only skipped sources.
    tiny_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
        "hQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    email = Email(
        internet_message_id="<only-skipped@fixture>",
        sender_address="x@vendor.example",
        sender_domain="vendor.example",
        subject="n/a",
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        body_html="",
        body_text="",
        status="new",
    )
    db_session.add(email)
    db_session.flush()
    att = Attachment(
        email_id=email.id,
        filename="logo.png",
        content_type="image/png",
        size=len(tiny_png),
        blob_url="",
        sha256="0" * 64,
    )
    db_session.add(att)
    db_session.flush()
    att.blob_url = store.put(f"{email.id}/{att.id}/logo.png", tiny_png)

    advance_once(db_session, store, FakeVisionExtractor())
    advance_once(db_session, store, FakeVisionExtractor())

    db_session.refresh(email)
    assert email.status == "error"
    assert email.error_detail == "no extractable content"


def test_one_bad_source_does_not_discard_healthy_siblings(db_session, store, monkeypatch):
    import io

    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "Payment advice INV-9 amount 1234.00 total due now please")
    c.showPage()
    c.save()

    email = Email(
        internet_message_id="<multi-source@fixture>",
        sender_address="x@vendor.example",
        sender_domain="vendor.example",
        subject="n/a",
        received_at=datetime(2026, 1, 2, tzinfo=UTC),
        body_html="<table><tr><td>INV-1</td><td>100.00</td></tr>"
        "<tr><td>INV-2</td><td>200.00</td></tr></table>",
        body_text="",
        status="new",
    )
    db_session.add(email)
    db_session.flush()
    att = Attachment(
        email_id=email.id,
        filename="advice.pdf",
        content_type="application/pdf",
        size=buf.getbuffer().nbytes,
        blob_url="",
        sha256="0" * 64,
    )
    db_session.add(att)
    db_session.flush()
    att.blob_url = store.put(f"{email.id}/{att.id}/advice.pdf", buf.getvalue())

    advance_once(db_session, store, FakeVisionExtractor())  # classify

    def boom(_data: bytes):
        raise RuntimeError("pdf extractor exploded")

    monkeypatch.setattr("ar_pipeline.pipeline.advance.extract_pdf", boom)
    advance_once(db_session, store, FakeVisionExtractor())  # extract

    db_session.refresh(email)
    assert email.status == "error"
    assert "pdf_text" in (email.error_detail or "")

    sources = db_session.scalars(
        select(ExtractionSource).where(ExtractionSource.email_id == email.id)
    ).all()
    by_kind = {s.kind: s for s in sources}
    # the healthy body_table source kept its RawExtraction row
    assert _raw_for(db_session, by_kind["body_table"].id) is not None
    assert _raw_for(db_session, by_kind["pdf_text"].id) is None


def _raw_for(session, source_id):
    return session.scalar(
        select(RawExtraction).where(RawExtraction.extraction_source_id == source_id)
    )


def test_poison_extractor_is_isolated_per_email(db_session, store, monkeypatch):
    excel_email = load_email("06_zenith_excel", db_session, store)
    body_email = load_email("05_bharat_body_freetext", db_session, store)

    # first pass: classify both
    advance_once(db_session, store, FakeVisionExtractor())
    db_session.refresh(excel_email)
    db_session.refresh(body_email)
    assert excel_email.status == "classified"
    assert body_email.status == "classified"

    def boom(_data: bytes):
        raise RuntimeError("excel extractor exploded")

    monkeypatch.setattr("ar_pipeline.pipeline.advance.extract_excel", boom)

    stats = advance_once(db_session, store, FakeVisionExtractor())

    assert stats.errored == 1
    db_session.refresh(excel_email)
    db_session.refresh(body_email)
    assert excel_email.status == "error"
    assert excel_email.error_detail
    assert "RuntimeError" in excel_email.error_detail
    # the other email in the same batch still advanced
    assert body_email.status == "extracted"

    # session is still usable after the isolated failure
    remaining = db_session.scalars(select(Email)).all()
    assert len(remaining) == 2
