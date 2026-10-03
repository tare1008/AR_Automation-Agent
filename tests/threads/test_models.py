from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from ar_pipeline.db.models import Email, EmailMessage, Extraction


def _email(db_session, mid: str = "m-1") -> Email:
    e = Email(
        internet_message_id=mid,
        sender_address="a@b.com",
        sender_domain="b.com",
        subject="s",
        received_at=datetime(2026, 10, 1, tzinfo=UTC),
        status="new",
        thread_key="thread-1",
    )
    db_session.add(e)
    db_session.flush()
    return e


def test_email_message_round_trip(db_session):
    e = _email(db_session)
    m = EmailMessage(
        email_id=e.id,
        position=1,
        sender="str@alufluoride.com",
        raw_header="From: x",
        is_internal=False,
        carries_attachments=False,
        body_text="hi",
        tables=[["a", "b"]],
        fingerprint="f" * 64,
        status="new",
    )
    db_session.add(m)
    db_session.flush()
    db_session.refresh(m)
    assert m.tables == [["a", "b"]]
    assert m.seen_reason is None


def test_new_extraction_statuses_are_allowed(db_session):
    e = _email(db_session)
    for status in ("already_recorded", "duplicate"):
        db_session.add(Extraction(email_id=e.id, canonical={}, status=status))
    db_session.flush()


def test_strong_payment_key_is_unique_among_live_rows(db_session):
    e = _email(db_session)
    db_session.add(
        Extraction(
            email_id=e.id,
            canonical={},
            status="approved",
            payment_key="utr:X1",
            payment_key_strength="strong",
        )
    )
    db_session.flush()
    db_session.add(
        Extraction(
            email_id=e.id,
            canonical={},
            status="pending_review",
            payment_key="utr:X1",
            payment_key_strength="strong",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_rejected_and_weak_keys_do_not_collide(db_session):
    e = _email(db_session)
    db_session.add_all(
        [
            Extraction(
                email_id=e.id,
                canonical={},
                status="rejected",
                payment_key="utr:X2",
                payment_key_strength="strong",
            ),
            Extraction(
                email_id=e.id,
                canonical={},
                status="pending_review",
                payment_key="utr:X2",
                payment_key_strength="strong",
            ),
            Extraction(
                email_id=e.id,
                canonical={},
                status="pending_review",
                payment_key="soft:a:1.00:nodate",
                payment_key_strength="weak",
            ),
            Extraction(
                email_id=e.id,
                canonical={},
                status="pending_review",
                payment_key="soft:a:1.00:nodate",
                payment_key_strength="weak",
            ),
        ]
    )
    db_session.flush()
