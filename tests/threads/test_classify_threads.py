from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

import ar_pipeline.config as config_module
from ar_pipeline.db.models import Email, EmailMessage, ExtractionSource
from ar_pipeline.pipeline.advance import advance_once
from ar_pipeline.storage import LocalBlobStore
from tests.extract.vision_fake import FakeVisionExtractor
from tests.normalize.llm_fake import FakeLLMClient
from tests.threads.chains import chain_html


def _email(db_session, html, mid="m-chain-1"):
    e = Email(
        internet_message_id=mid,
        sender_address="dharmendra.p.kumar-c@adityabirla.com",
        sender_domain="adityabirla.com",
        subject="FW: Payment Remittance details",
        received_at=datetime(2026, 2, 18, 12, 8, tzinfo=UTC),
        body_html=html,
        body_text="",
        status="new",
    )
    db_session.add(e)
    db_session.flush()
    return e


def test_classify_splits_and_creates_one_source_per_new_message(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("CLIENT_DOMAINS", "adityabirla.com")
    config_module.get_settings.cache_clear()
    try:
        e = _email(db_session, chain_html([3, 2, 1]))
        advance_once(
            db_session, LocalBlobStore(str(tmp_path)), FakeVisionExtractor(), FakeLLMClient()
        )
        msgs = db_session.scalars(
            select(EmailMessage)
            .where(EmailMessage.email_id == e.id)
            .order_by(EmailMessage.position)
        ).all()
        assert [m.status for m in msgs] == ["no_content", "new", "new", "new"]
        srcs = db_session.scalars(
            select(ExtractionSource).where(ExtractionSource.email_id == e.id)
        ).all()
        assert sorted(s.kind for s in srcs) == ["body_table"] * 3
        assert {s.email_message_id for s in srcs} == {m.id for m in msgs[1:]}
        assert msgs[0].body_text is not None  # no_content still keeps its own text
    finally:
        config_module.get_settings.cache_clear()


def test_extract_reads_the_message_not_the_whole_email(db_session, tmp_path):
    e = _email(db_session, chain_html([2, 1]), mid="m-chain-2")
    store = LocalBlobStore(str(tmp_path))
    advance_once(db_session, store, FakeVisionExtractor(), FakeLLMClient())  # classify
    advance_once(db_session, store, FakeVisionExtractor(), FakeLLMClient())  # extract
    from ar_pipeline.db.models import RawExtraction

    payloads = db_session.scalars(
        select(RawExtraction.payload)
        .join(ExtractionSource)
        .where(ExtractionSource.email_id == e.id)
    ).all()
    assert len(payloads) == 2
    for p in payloads:
        assert p["text"].count("PUNBR52026") == 1  # each source holds one message
        assert len(p["tables"]) == 1


def test_splitter_failure_falls_back_to_one_message(db_session, tmp_path, monkeypatch):
    import ar_pipeline.pipeline.advance as advance_module

    def boom(**_kwargs):
        raise RuntimeError("splitter exploded")

    monkeypatch.setattr(advance_module, "split_email", boom)
    e = _email(db_session, chain_html([2, 1]), mid="m-chain-boom")
    advance_once(db_session, LocalBlobStore(str(tmp_path)), FakeVisionExtractor(), FakeLLMClient())
    db_session.refresh(e)
    assert e.status == "classified"
    msgs = db_session.scalars(select(EmailMessage).where(EmailMessage.email_id == e.id)).all()
    assert len(msgs) == 1
    assert msgs[0].position == 0
    assert msgs[0].status == "new"
    assert msgs[0].tables == []
    assert msgs[0].body_text
