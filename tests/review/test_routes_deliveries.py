import uuid
from datetime import UTC, datetime

import pytest

from ar_pipeline.db.models import Delivery


@pytest.fixture
def failed_delivery(db_session, seed_pending):
    email, ext = seed_pending()
    ext.status = "approved"
    d = Delivery(
        extraction_id=ext.id,
        status="failed",
        attempts=6,
        last_attempt_at=datetime(2026, 9, 10, tzinfo=UTC),
        last_error="500: boom",
    )
    db_session.add(d)
    db_session.flush()
    return d


def test_errors_page_lists_failed_deliveries(client, failed_delivery):
    r = client.get("/review/errors")
    assert r.status_code == 200
    assert "500: boom" in r.text
    assert f"/review/deliveries/{failed_delivery.id}/resend" in r.text


def test_resend_requeues_the_delivery(client, failed_delivery, db_session):
    r = client.post(f"/review/deliveries/{failed_delivery.id}/resend")
    assert r.status_code == 303
    db_session.refresh(failed_delivery)
    assert failed_delivery.status == "pending"
    assert failed_delivery.attempts == 0
    assert failed_delivery.last_error is None


def test_resend_unknown_id_flashes(client):
    r = client.post(f"/review/deliveries/{uuid.uuid4()}/resend")
    assert r.status_code == 303
    assert "flash=" in r.headers["location"]


def test_resend_non_failed_delivery_flashes(client, failed_delivery, db_session):
    failed_delivery.status = "delivered"
    db_session.flush()
    r = client.post(f"/review/deliveries/{failed_delivery.id}/resend")
    assert r.status_code == 303
    assert "flash=" in r.headers["location"]
    db_session.refresh(failed_delivery)
    assert failed_delivery.status == "delivered"
