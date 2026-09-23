from __future__ import annotations

import uuid
from datetime import UTC, datetime

from ar_pipeline.db.models import Email


def _email(db_session, *, status: str) -> Email:
    email = Email(
        internet_message_id=f"m-{uuid.uuid4()}",
        sender_address="ap@vendor.com",
        sender_domain="vendor.com",
        subject="Payment advice mid-flight",
        received_at=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
        body_html="<p>x</p>",
        body_text="x",
        status=status,
    )
    db_session.add(email)
    db_session.flush()
    return email


def test_journey_rows_is_a_bare_fragment_not_a_full_page(client, seed_pending):
    seed_pending()
    r = client.get("/review/journey-rows")
    assert r.status_code == 200
    assert "<!doctype" not in r.text.lower()
    assert "<header" not in r.text.lower()
    assert "stat-row" in r.text


def test_journey_rows_requires_login():
    from fastapi.testclient import TestClient

    from ar_pipeline.main import app

    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get("/review/journey-rows").status_code == 303


def test_journey_rows_shows_in_progress_for_email_with_no_extraction_yet(client, db_session):
    _email(db_session, status="classified")
    r = client.get("/review/journey-rows")
    assert "Extraction in progress" in r.text


def test_journey_rows_does_not_call_errored_email_in_progress(client, db_session):
    _email(db_session, status="error")
    r = client.get("/review/journey-rows")
    assert "Extraction in progress" not in r.text


def test_queue_rows_is_a_bare_fragment(client, seed_pending):
    email, _ext = seed_pending()
    r = client.get("/review/queue-rows")
    assert r.status_code == 200
    assert "<!doctype" not in r.text.lower()
    assert email.subject in r.text


def test_queue_rows_requires_login():
    from fastapi.testclient import TestClient

    from ar_pipeline.main import app

    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get("/review/queue-rows").status_code == 303


def test_approved_rows_is_bare_fragment_and_respects_auto_filter(client, db_session, seed_pending):
    from ar_pipeline.db.models import Delivery

    e1, x1 = seed_pending()
    x1.status = "approved"
    x1.reviewed_by = "auto"
    e2, x2 = seed_pending()
    x2.status = "approved"
    x2.reviewed_by = "Priya"
    db_session.add(Delivery(extraction_id=x1.id, status="pending"))
    db_session.add(Delivery(extraction_id=x2.id, status="pending"))
    db_session.flush()

    r_all = client.get("/review/approved-rows")
    assert "<!doctype" not in r_all.text.lower()
    assert "Auto" in r_all.text
    assert "Priya" in r_all.text

    r_auto = client.get("/review/approved-rows?by=auto")
    assert "Auto" in r_auto.text
    assert "Priya" not in r_auto.text


def test_approved_rows_requires_login():
    from fastapi.testclient import TestClient

    from ar_pipeline.main import app

    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get("/review/approved-rows").status_code == 303
