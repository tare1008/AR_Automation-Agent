from __future__ import annotations

from sqlalchemy import select

from ar_pipeline.db.models import EmailMessage, ExtractionSource
from ar_pipeline.threads.backfill import backfill
from tests.review import conftest as _review_conftest

seed_pending = _review_conftest.seed_pending  # reuse the review fixture


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
