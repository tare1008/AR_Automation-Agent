from __future__ import annotations

import pytest
from sqlalchemy import select

from ar_pipeline.db.models import Email, ExtractionSource, RawExtraction
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
