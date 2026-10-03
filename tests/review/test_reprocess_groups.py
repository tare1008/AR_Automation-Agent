"""Reprocess re-normalizes each affected message group (R23)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from ar_pipeline.db.models import Email, EmailMessage, Extraction, ExtractionSource, RawExtraction
from ar_pipeline.normalize.service import normalize_one
from ar_pipeline.review.service import ReviewError, reprocess_email
from tests.normalize.llm_fake import FakeLLMClient
from tests.review.conftest import _canonical
from tests.threads.test_normalize_threads import _out


def _email(db_session) -> Email:
    e = Email(
        internet_message_id=f"rp-{uuid.uuid4()}",
        sender_address="ap@payer.example",
        sender_domain="payer.example",
        subject="payment advice",
        received_at=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
        body_text="advice",
        status="review",
    )
    db_session.add(e)
    db_session.flush()
    return e


def _group(
    db_session, email: Email, *, legacy: bool = False, position: int = 0
) -> EmailMessage | None:
    msg = None
    if not legacy:
        msg = EmailMessage(
            email_id=email.id,
            position=position,
            raw_header="",
            is_internal=False,
            carries_attachments=False,
            status="new",
        )
        db_session.add(msg)
        db_session.flush()
    src = ExtractionSource(
        email_id=email.id,
        kind="body_text",
        ref="body",
        email_message_id=msg.id if msg else None,
    )
    db_session.add(src)
    db_session.flush()
    db_session.add(RawExtraction(extraction_source_id=src.id, payload={"text": "paid INV-1"}))
    db_session.flush()
    return msg


def _row(db_session, email, msg, status, ref):
    canonical = _canonical()
    canonical["header"]["payment_reference"] = ref
    x = Extraction(
        email_id=email.id,
        email_message_id=msg.id if msg else None,
        canonical=canonical,
        is_remittance=True,
        validation_flags=[],
        status=status,
    )
    db_session.add(x)
    db_session.flush()
    return x


def _reprocess_and_normalize(db_session, email):
    reprocess_email(db_session, email.id)
    llm = FakeLLMClient(responses=[_out(7)])
    normalize_one(db_session, email, llm)
    rows = db_session.scalars(select(Extraction).where(Extraction.email_id == email.id)).all()
    return llm, rows


def _assert_renormalized(llm, rows, old):
    assert len(llm.calls) == 1
    assert {r.status for r in rows if r.id in old} == {"superseded"}
    fresh = [r for r in rows if r.id not in old]
    assert len(fresh) == 1 and fresh[0].status == "pending_review"


def test_a_rejected_sibling_is_superseded_and_the_message_renormalized(db_session):
    e = _email(db_session)
    m = _group(db_session, e)
    a = _row(db_session, e, m, "pending_review", "UTR-A")
    b = _row(db_session, e, m, "rejected", "UTR-B")
    llm, rows = _reprocess_and_normalize(db_session, e)
    _assert_renormalized(llm, rows, {a.id, b.id})


def test_b_a_duplicate_sibling_is_superseded_and_the_message_renormalized(db_session):
    e = _email(db_session)
    m = _group(db_session, e)
    a = _row(db_session, e, m, "pending_review", "UTR-A")
    b = _row(db_session, e, m, "duplicate", "UTR-B")
    llm, rows = _reprocess_and_normalize(db_session, e)
    _assert_renormalized(llm, rows, {a.id, b.id})


def test_c_legacy_group_with_pending_and_rejected_is_renormalized(db_session):
    e = _email(db_session)
    _group(db_session, e, legacy=True)
    a = _row(db_session, e, None, "pending_review", "UTR-A")
    b = _row(db_session, e, None, "rejected", "UTR-B")
    llm, rows = _reprocess_and_normalize(db_session, e)
    _assert_renormalized(llm, rows, {a.id, b.id})


def test_d_group_with_already_recorded_row_is_refused(db_session):
    e = _email(db_session)
    m = _group(db_session, e)
    a = _row(db_session, e, m, "pending_review", "UTR-A")
    b = _row(db_session, e, m, "already_recorded", "UTR-B")
    with pytest.raises(ReviewError, match="cannot reprocess"):
        reprocess_email(db_session, e.id)
    db_session.refresh(a)
    db_session.refresh(b)
    assert (a.status, b.status) == ("pending_review", "already_recorded")


def test_other_message_groups_are_left_alone(db_session):
    e = _email(db_session)
    m1 = _group(db_session, e)
    m2 = _group(db_session, e, position=1)
    a = _row(db_session, e, m1, "pending_review", "UTR-A")
    done = _row(db_session, e, m2, "approved", "UTR-C")  # a different message: not blocking
    rejected = _row(db_session, e, m2, "rejected", "UTR-D")
    reprocess_email(db_session, e.id)
    for x in (a, done, rejected):
        db_session.refresh(x)
    assert (a.status, done.status, rejected.status) == ("superseded", "approved", "rejected")
