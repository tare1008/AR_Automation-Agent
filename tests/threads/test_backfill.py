from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import ar_pipeline.config as config_module
from ar_pipeline.db.models import Email, EmailMessage, ExtractionSource
from ar_pipeline.threads.backfill import backfill
from tests.normalize.llm_fake import FakeLLMClient
from tests.threads.chains import chain_html
from tests.threads.test_normalize_threads import _ingest, _out, _run


def test_backfill_creates_messages_and_keys_and_flags_conflicts(db_session, seed_pending):
    e1, x1 = seed_pending()
    e2, x2 = seed_pending()  # same canonical -> same UTR "UTR-1"
    x1.status = "approved"
    db_session.add(ExtractionSource(email_id=e1.id, kind="body_text", ref="body"))
    db_session.flush()
    created, keyed, conflicts = backfill(db_session)
    assert (created, keyed, conflicts) == (2, 1, 1)
    m1 = db_session.scalar(select(EmailMessage).where(EmailMessage.email_id == e1.id))
    assert m1.position == 0 and m1.status == "new"
    assert (
        db_session.scalar(
            select(ExtractionSource.email_message_id).where(ExtractionSource.email_id == e1.id)
        )
        == m1.id
    )
    db_session.refresh(x1)
    db_session.refresh(x2)
    assert x1.payment_key == "utr:UTR1" and x2.payment_key is None
    assert x2.duplicate_of_id == x1.id
    assert x2.validation_flags[-1].startswith("header: possible duplicate")
    assert backfill(db_session) == (0, 0, 0)


def test_two_approved_rows_sharing_a_utr_never_violate_the_index(db_session, seed_pending):
    _, a = seed_pending()
    _, b = seed_pending()
    a.status = b.status = "approved"
    db_session.flush()
    _, keyed, conflicts = backfill(db_session)
    assert (keyed, conflicts) == (1, 1)
    db_session.refresh(a)
    db_session.refresh(b)
    assert sorted([a.payment_key is None, b.payment_key is None]) == [False, True]


def test_non_live_rows_sharing_a_key_both_just_get_it(db_session, seed_pending):
    _, a = seed_pending()
    _, b = seed_pending()
    a.status = b.status = "rejected"
    db_session.flush()
    _, keyed, conflicts = backfill(db_session)
    assert (keyed, conflicts) == (2, 0)
    db_session.refresh(a)
    db_session.refresh(b)
    assert a.payment_key == b.payment_key == "utr:UTR1"


def _bare_email(db_session, status, mid):
    e = Email(
        internet_message_id=mid,
        sender_address="ap@payer.example",
        sender_domain="payer.example",
        subject="payment advice",
        received_at=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
        body_text="advice",
        status=status,
    )
    db_session.add(e)
    db_session.flush()
    return e


@pytest.mark.parametrize("status", ["new", "classified", "error"])
def test_in_flight_emails_are_left_alone(db_session, status):
    e = _bare_email(db_session, status, f"bf-{status}")
    src = ExtractionSource(email_id=e.id, kind="body_text", ref="body")
    db_session.add(src)
    db_session.flush()
    assert backfill(db_session) == (0, 0, 0)
    assert db_session.scalars(select(EmailMessage).where(EmailMessage.email_id == e.id)).all() == []
    db_session.refresh(src)
    assert src.email_message_id is None


def test_a_new_email_is_split_properly_after_backfill(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("CLIENT_DOMAINS", "adityabirla.com")
    config_module.get_settings.cache_clear()
    try:
        e = _ingest(db_session, chain_html([2, 1]), "bf-chain")
        backfill(db_session)
        llm = FakeLLMClient(responses=[_out(1), _out(2)])
        _run(db_session, tmp_path, llm)
    finally:
        config_module.get_settings.cache_clear()
    db_session.refresh(e)
    assert e.status != "error"
    assert len(llm.calls) == 2
    msgs = db_session.scalars(select(EmailMessage).where(EmailMessage.email_id == e.id)).all()
    assert len(msgs) > 1
