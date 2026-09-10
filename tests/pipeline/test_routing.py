from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from ar_pipeline.db.models import Delivery, Email, Extraction
from ar_pipeline.pipeline.routing import AUTO_REVIEWER, approve_and_queue, settle_email


def _email(db_session, status="review") -> Email:
    e = Email(
        internet_message_id=f"m-{uuid.uuid4()}",
        sender_address="a@b.com",
        sender_domain="b.com",
        subject="s",
        received_at=datetime(2026, 9, 1, tzinfo=UTC),
        status=status,
    )
    db_session.add(e)
    db_session.flush()
    return e


def _ext(db_session, email, *, status="pending_review", canonical=None) -> Extraction:
    x = Extraction(
        email_id=email.id,
        status=status,
        is_remittance=True,
        canonical=canonical if canonical is not None else {"envelope": {"extraction_id": "x"}},
        confidence=Decimal("0.9"),
    )
    db_session.add(x)
    db_session.flush()
    return x


def test_approve_and_queue_sets_status_stamps_reviewer_and_inserts_delivery(db_session):
    email = _email(db_session)
    ext = _ext(db_session, email)
    approve_and_queue(db_session, ext, reviewed_by=AUTO_REVIEWER)
    db_session.refresh(ext)
    assert ext.status == "approved"
    assert ext.reviewed_by == "auto"
    assert ext.canonical["envelope"]["reviewed_by"] == "auto"
    deliveries = db_session.scalars(select(Delivery).where(Delivery.extraction_id == ext.id)).all()
    assert len(deliveries) == 1 and deliveries[0].status == "pending"


def test_approve_and_queue_tolerates_empty_canonical(db_session):
    email = _email(db_session)
    ext = _ext(db_session, email, canonical={})
    approve_and_queue(db_session, ext, reviewed_by="Asha")  # no envelope -> no crash
    db_session.refresh(ext)
    assert ext.status == "approved"


def test_settle_email_marks_done_when_nothing_pending(db_session):
    email = _email(db_session)
    _ext(db_session, email, status="approved")
    settle_email(db_session, email)
    db_session.refresh(email)
    assert email.status == "done"


def test_settle_email_leaves_review_when_a_pending_row_remains(db_session):
    email = _email(db_session)
    _ext(db_session, email, status="approved")
    _ext(db_session, email, status="pending_review")
    settle_email(db_session, email)
    db_session.refresh(email)
    assert email.status == "review"
