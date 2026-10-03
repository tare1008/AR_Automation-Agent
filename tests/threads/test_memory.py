from __future__ import annotations

from datetime import UTC, datetime

from ar_pipeline.db.models import Email, EmailMessage, Extraction
from ar_pipeline.threads.memory import (
    all_references_recorded,
    find_seen_by_fingerprint,
    message_is_recorded,
)


def _email(db_session, mid):
    e = Email(
        internet_message_id=mid,
        sender_address="a@b.com",
        sender_domain="b.com",
        subject="s",
        received_at=datetime(2026, 10, 1, tzinfo=UTC),
        status="review",
    )
    db_session.add(e)
    db_session.flush()
    return e


def _msg(db_session, email, fp="f" * 64, status="new"):
    m = EmailMessage(
        email_id=email.id,
        position=1,
        raw_header="",
        is_internal=False,
        carries_attachments=False,
        fingerprint=fp,
        status=status,
    )
    db_session.add(m)
    db_session.flush()
    return m


def _ext(db_session, email, msg, status, *, remit=True, key=None):
    x = Extraction(
        email_id=email.id,
        email_message_id=msg.id,
        canonical={},
        status=status,
        is_remittance=remit,
        payment_key=key,
        payment_key_strength="strong" if key else None,
    )
    db_session.add(x)
    db_session.flush()
    return x


def test_recorded_rules(db_session):
    e = _email(db_session, "m1")
    m = _msg(db_session, e)
    assert not message_is_recorded(db_session, m.id)  # no extraction yet
    _ext(db_session, e, m, "pending_review")
    assert message_is_recorded(db_session, m.id)
    _ext(db_session, e, m, "rejected")  # bad extraction rejected -> not recorded
    assert not message_is_recorded(db_session, m.id)


def test_not_a_remittance_rejection_counts(db_session):
    e = _email(db_session, "m2")
    m = _msg(db_session, e)
    _ext(db_session, e, m, "rejected", remit=False)
    assert message_is_recorded(db_session, m.id)


def test_seen_by_fingerprint_skips_own_email_and_unrecorded(db_session):
    first = _email(db_session, "m3")
    m1 = _msg(db_session, first, fp="a" * 64)
    second = _email(db_session, "m4")
    assert find_seen_by_fingerprint(db_session, email_id=second.id, fingerprint="a" * 64) is None
    _ext(db_session, first, m1, "approved")
    assert (
        find_seen_by_fingerprint(db_session, email_id=second.id, fingerprint="a" * 64).id == m1.id
    )
    assert find_seen_by_fingerprint(db_session, email_id=first.id, fingerprint="a" * 64) is None


def test_all_references_recorded(db_session):
    e = _email(db_session, "m5")
    m = _msg(db_session, e)
    _ext(db_session, e, m, "approved", key="utr:REF11111111")
    assert all_references_recorded(db_session, {"REF11111111"})
    assert not all_references_recorded(db_session, {"REF11111111", "REF22222222"})
    assert not all_references_recorded(db_session, set())


def test_superseded_rows_do_not_count_toward_recorded(db_session):
    e = _email(db_session, "m-sup")
    only_superseded = _msg(db_session, e)
    _ext(db_session, e, only_superseded, "superseded")
    assert not message_is_recorded(db_session, only_superseded.id)
    e2 = _email(db_session, "m-sup2")
    mixed = _msg(db_session, e2)
    _ext(db_session, e2, mixed, "superseded")
    _ext(db_session, e2, mixed, "approved")
    assert message_is_recorded(db_session, mixed.id)
