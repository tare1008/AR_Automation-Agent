from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from ar_pipeline.db.models import Attachment, Email, Extraction


def _make_email(db_session, mid: str) -> Email:
    email = Email(
        internet_message_id=mid,
        sender_address="a@v.com",
        sender_domain="v.com",
        subject="x",
        received_at=datetime(2026, 9, 9, tzinfo=UTC),
        body_html="",
        body_text="",
        raw_headers={},
        status="new",
    )
    db_session.add(email)
    db_session.flush()
    return email


def test_insert_and_query_email(db_session):
    email = Email(
        internet_message_id="<msg-1@vendor.com>",
        sender_address="ap@vendor.com",
        sender_domain="vendor.com",
        subject="Remittance",
        received_at=datetime(2026, 9, 9, tzinfo=UTC),
        body_html="<p>hi</p>",
        body_text="hi",
        raw_headers={},
        status="new",
    )
    db_session.add(email)
    db_session.flush()
    assert email.id is not None

    got = db_session.get(Email, email.id)
    assert got.sender_domain == "vendor.com"
    assert got.status == "new"


def test_internet_message_id_is_unique(db_session):
    for _ in range(2):
        db_session.add(
            Email(
                internet_message_id="<dupe@vendor.com>",
                sender_address="ap@vendor.com",
                sender_domain="vendor.com",
                subject="x",
                received_at=datetime(2026, 9, 9, tzinfo=UTC),
                body_html="",
                body_text="",
                raw_headers={},
                status="new",
            )
        )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_bad_status_rejected(db_session):
    db_session.add(
        Email(
            internet_message_id="<bad-status@vendor.com>",
            sender_address="ap@vendor.com",
            sender_domain="vendor.com",
            subject="x",
            received_at=datetime(2026, 9, 9, tzinfo=UTC),
            body_html="",
            body_text="",
            raw_headers={},
            status="banana",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_attachment_belongs_to_email(db_session):
    email = Email(
        internet_message_id="<att@vendor.com>",
        sender_address="ap@vendor.com",
        sender_domain="vendor.com",
        subject="x",
        received_at=datetime(2026, 9, 9, tzinfo=UTC),
        body_html="",
        body_text="",
        raw_headers={},
        status="new",
    )
    db_session.add(email)
    db_session.flush()
    att = Attachment(
        email_id=email.id,
        filename="settlement.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size=1234,
        blob_url="file://data/blob/abc",
        sha256="0" * 64,
    )
    db_session.add(att)
    db_session.flush()
    assert att.id is not None


def test_jsonb_in_place_mutation_persists(db_session):
    email = Email(
        internet_message_id="<jsonb@vendor.com>",
        sender_address="a@v.com",
        sender_domain="v.com",
        subject="x",
        received_at=datetime(2026, 9, 9, tzinfo=UTC),
        body_html="",
        body_text="",
        raw_headers={"a": 1},
        status="new",
    )
    db_session.add(email)
    db_session.flush()
    email.raw_headers["b"] = 2
    db_session.flush()
    db_session.expire(email)
    assert db_session.get(Email, email.id).raw_headers == {"a": 1, "b": 2}


def test_confidence_out_of_range_rejected(db_session):
    email = _make_email(db_session, "<conf@vendor.com>")
    db_session.add(
        Extraction(
            email_id=email.id,
            canonical={},
            confidence=Decimal("1.5"),
            validation_flags=[],
            status="pending_review",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
