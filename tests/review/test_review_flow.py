from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

from ar_pipeline.db.models import Extraction


def _seed_in_order(db_session, seed_pending, n, **kw):
    """Seed n pending items with distinct arrival times, oldest first — the
    queue order is by arrival, so tests must not rely on insertion order."""
    out = []
    base = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    for i in range(n):
        email, ext = seed_pending(**kw)
        email.received_at = base + timedelta(minutes=i)
        out.append(ext)
    db_session.flush()
    return out


def _approve_form() -> dict[str, str]:
    return {
        "header.payer_name": "Acme Corp",
        "header.currency": "INR",
        "header.total_paid_amount": "90.00",
        "line_items[0].invoice_number": "INV-1",
        "line_items[0].invoice_amount": "100.00",
        "line_items[0].amount_paid": "90.00",
        "line_items[0].deductions[0].type": "tds",
        "line_items[0].deductions[0].amount": "10.00",
        "line_items[0].deductions[0].reason": "194Q",
        "approve": "1",
    }


def test_approve_goes_straight_to_the_next_pending_item(client, db_session, seed_pending):
    first, second = _seed_in_order(db_session, seed_pending, 2)
    r = client.post(f"/review/{first.id}/edit", data=_approve_form())
    location = unquote(r.headers["location"])
    assert location.startswith(f"/review/{second.id}?flash=")
    assert "1 left in queue" in location


def test_approving_the_last_item_returns_to_an_empty_queue(client, seed_pending):
    _e, only = seed_pending()
    r = client.post(f"/review/{only.id}/edit", data=_approve_form())
    location = unquote(r.headers["location"])
    assert location.startswith("/review/queue?flash=")
    assert "queue clear" in location


def test_reject_goes_straight_to_the_next_pending_item(client, db_session, seed_pending):
    first, second = _seed_in_order(db_session, seed_pending, 2)
    r = client.post(f"/review/{first.id}/reject", data={"reason": "dup"})
    assert unquote(r.headers["location"]).startswith(f"/review/{second.id}?flash=")


def test_approve_keeps_queue_position_rather_than_jumping_to_the_oldest(
    client, db_session, seed_pending
):
    _first, middle, last = _seed_in_order(db_session, seed_pending, 3)
    r = client.post(f"/review/{middle.id}/edit", data=_approve_form())
    assert unquote(r.headers["location"]).startswith(f"/review/{last.id}?flash=")


def test_skip_moves_to_the_next_item_without_acting(client, db_session, seed_pending):
    first, second = _seed_in_order(db_session, seed_pending, 2)
    r = client.get(f"/review/{first.id}/next")
    assert r.headers["location"] == f"/review/{second.id}"
    db_session.refresh(first)
    assert first.status == "pending_review"


def test_skip_wraps_around_to_the_start_of_the_queue(client, db_session, seed_pending):
    first, second = _seed_in_order(db_session, seed_pending, 2)
    r = client.get(f"/review/{second.id}/next")
    assert r.headers["location"] == f"/review/{first.id}"


def test_skip_with_nothing_else_pending_stays_put(client, seed_pending):
    _e, only = seed_pending()
    r = client.get(f"/review/{only.id}/next")
    location = unquote(r.headers["location"])
    assert location.startswith(f"/review/{only.id}?flash=")
    assert "Nothing else" in location


def test_review_screen_has_a_skip_link(client, seed_pending):
    _e, ext = seed_pending()
    assert f"/review/{ext.id}/next" in client.get(f"/review/{ext.id}").text


def test_bulk_reject_only_touches_not_a_remittance_items(client, db_session, seed_pending):
    _e1, junk = seed_pending(is_remittance=False)
    _e2, real = seed_pending()
    r = client.post(
        "/review/bulk-reject",
        data={"extraction_id": [str(junk.id), str(real.id)], "reason": "Not a remittance"},
    )
    assert r.status_code == 303
    assert "1 rejected" in unquote(r.headers["location"])
    db_session.expire_all()
    assert db_session.get(Extraction, junk.id).status == "rejected"
    assert db_session.get(Extraction, real.id).status == "pending_review"


def test_bulk_reject_requires_a_reason(client, db_session, seed_pending):
    _e, junk = seed_pending(is_remittance=False)
    r = client.post("/review/bulk-reject", data={"extraction_id": [str(junk.id)], "reason": " "})
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Extraction, junk.id).status == "pending_review"


def test_queue_offers_checkboxes_only_on_not_a_remittance_rows(client, seed_pending):
    _e1, junk = seed_pending(is_remittance=False)
    _e2, real = seed_pending()
    text = client.get("/review/queue").text
    assert f'value="{junk.id}"' in text
    assert f'value="{real.id}"' not in text
    assert "/review/bulk-reject" in text


def test_queue_hides_bulk_reject_when_nothing_qualifies(client, seed_pending):
    seed_pending()
    assert "/review/bulk-reject" not in client.get("/review/queue").text
