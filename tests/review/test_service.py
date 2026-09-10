import uuid

import pytest
from sqlalchemy import select

from ar_pipeline.db.models import Delivery, ExtractionEdit
from ar_pipeline.review.auth import User
from ar_pipeline.review.service import (
    ReviewError,
    approve_extraction,
    list_errored,
    list_pending,
    load_detail,
    reject_extraction,
    reprocess_email,
    retry_email,
    save_edits,
)

U = User(name="Asha Rao")


def test_list_pending_orders_by_received_then_created(db_session, seed_pending):
    e1, x1 = seed_pending()
    e2, x2 = seed_pending()
    e2.received_at = e1.received_at.replace(year=2025)
    db_session.flush()
    rows = list_pending(db_session)
    assert [r.extraction_id for r in rows][0] == x2.id  # 2025 email first


def test_approve_sets_status_and_inserts_delivery(db_session, seed_pending):
    email, ext = seed_pending()
    approve_extraction(db_session, ext.id, U)
    db_session.refresh(ext)
    db_session.refresh(email)
    assert ext.status == "approved"
    assert ext.reviewed_by == "Asha Rao"
    assert email.status == "done"
    deliveries = db_session.scalars(select(Delivery).where(Delivery.extraction_id == ext.id)).all()
    assert len(deliveries) == 1 and deliveries[0].status == "pending"


def test_approve_rejected_when_not_remittance(db_session, seed_pending):
    email, ext = seed_pending(is_remittance=False, canonical={})
    with pytest.raises(ReviewError):
        approve_extraction(db_session, ext.id, U)


def test_reject_requires_reason(db_session, seed_pending):
    email, ext = seed_pending()
    with pytest.raises(ReviewError):
        reject_extraction(db_session, ext.id, U, "   ")
    reject_extraction(db_session, ext.id, U, "duplicate of last week")
    db_session.refresh(ext)
    assert ext.status == "rejected"
    assert ext.reject_reason == "duplicate of last week"


def test_save_edits_writes_one_audit_row_per_leaf(db_session, seed_pending):
    email, ext = seed_pending()
    form = {
        "header.payer_name": "Acme Corporation",  # changed
        "header.payer_id": "",
        "header.payment_reference": "UTR-1",
        "header.payment_reference_type": "utr",
        "header.payment_date": "2026-09-05",
        "header.payment_method": "RTGS",
        "header.currency": "INR",
        "header.total_paid_amount": "90.00",
        "line_items[0].invoice_number": "INV-1",
        "line_items[0].invoice_date": "2026-08-01",
        "line_items[0].invoice_amount": "100.00",
        "line_items[0].amount_paid": "90.00",
        "line_items[0].deductions[0].type": "tds",
        "line_items[0].deductions[0].amount": "10.00",
        "line_items[0].deductions[0].reason": "194Q",
    }
    from ar_pipeline.review.forms import parse_form_to_canonical

    edits = save_edits(db_session, ext.id, U, parse_form_to_canonical(form), approve=False)
    assert ("header.payer_name", "Acme Corp", "Acme Corporation") in edits
    rows = db_session.scalars(
        select(ExtractionEdit).where(ExtractionEdit.extraction_id == ext.id)
    ).all()
    assert {r.field_path for r in rows} == {"header.payer_name"}
    assert rows[0].edited_by == "Asha Rao"


def test_save_edits_invalid_canonical_raises_and_writes_nothing(db_session, seed_pending):
    email, ext = seed_pending()
    bad = {"header": {"payer_name": "Acme"}, "line_items": []}  # min_length=1 violated
    with pytest.raises(ReviewError):
        save_edits(db_session, ext.id, U, bad, approve=False)
    rows = db_session.scalars(
        select(ExtractionEdit).where(ExtractionEdit.extraction_id == ext.id)
    ).all()
    assert rows == []


def test_save_edits_with_approve_true_approves(db_session, seed_pending):
    email, ext = seed_pending()
    from ar_pipeline.review.forms import parse_form_to_canonical

    form = {
        "header.payer_name": "Acme Corp",
        "header.currency": "INR",
        "header.total_paid_amount": "90.00",
        "line_items[0].invoice_number": "INV-1",
        "line_items[0].invoice_amount": "100.00",
        "line_items[0].amount_paid": "90.00",
        "line_items[0].deductions[0].type": "tds",
        "line_items[0].deductions[0].amount": "10.00",
    }
    save_edits(db_session, ext.id, U, parse_form_to_canonical(form), approve=True)
    db_session.refresh(ext)
    assert ext.status == "approved"


def test_save_edits_approve_true_on_non_remittance_writes_nothing(db_session, seed_pending):
    from ar_pipeline.review.forms import parse_form_to_canonical

    email, ext = seed_pending(is_remittance=False, canonical={})
    form = {
        "header.payer_name": "Acme",
        "header.currency": "INR",
        "header.total_paid_amount": "10.00",
        "line_items[0].invoice_number": "INV-1",
        "line_items[0].invoice_amount": "10.00",
        "line_items[0].amount_paid": "10.00",
    }
    with pytest.raises(ReviewError):
        save_edits(db_session, ext.id, U, parse_form_to_canonical(form), approve=True)
    from sqlalchemy import select

    from ar_pipeline.db.models import ExtractionEdit

    assert (
        db_session.scalars(
            select(ExtractionEdit).where(ExtractionEdit.extraction_id == ext.id)
        ).all()
        == []
    )


def test_reprocess_supersedes_and_reopens_email(db_session, seed_pending):
    email, ext = seed_pending()
    reprocess_email(db_session, email.id)
    db_session.refresh(ext)
    db_session.refresh(email)
    assert ext.status == "superseded"
    assert email.status == "classified"


def test_retry_errored_email_without_sources_goes_new(db_session, seed_pending):
    email, ext = seed_pending()
    email.status = "error"
    email.error_detail = "boom"
    db_session.flush()
    retry_email(db_session, email.id)
    db_session.refresh(email)
    assert email.status == "new"
    assert email.error_detail is None


def test_load_detail_missing_raises(db_session):
    with pytest.raises(ReviewError):
        load_detail(db_session, uuid.uuid4())


def test_list_errored_returns_error_emails_oldest_first(db_session, seed_pending):
    email, ext = seed_pending()
    email.status = "error"
    db_session.flush()
    assert [e.id for e in list_errored(db_session)] == [email.id]


def test_resend_delivery_resets_the_row(db_session, seed_pending):
    from datetime import UTC, datetime

    from ar_pipeline.db.models import Delivery
    from ar_pipeline.review.service import resend_delivery

    email, ext = seed_pending()
    ext.status = "approved"
    d = Delivery(
        extraction_id=ext.id,
        status="failed",
        attempts=6,
        last_error="boom",
        last_attempt_at=datetime(2026, 9, 10, tzinfo=UTC),
    )
    db_session.add(d)
    db_session.flush()
    resend_delivery(db_session, d.id)
    db_session.refresh(d)
    assert d.status == "pending" and d.attempts == 0 and d.last_error is None


def test_resend_delivery_rejects_non_failed(db_session, seed_pending):
    import pytest

    from ar_pipeline.db.models import Delivery
    from ar_pipeline.review.service import ReviewError, resend_delivery

    email, ext = seed_pending()
    d = Delivery(extraction_id=ext.id, status="pending", attempts=0)
    db_session.add(d)
    db_session.flush()
    with pytest.raises(ReviewError):
        resend_delivery(db_session, d.id)
