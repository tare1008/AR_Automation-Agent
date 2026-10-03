from __future__ import annotations

from ar_pipeline.db.models import EmailMessage


def _msg(db_session, email, position, status, **kw):
    m = EmailMessage(
        email_id=email.id,
        position=position,
        raw_header="From: x\nSent: y",
        is_internal=False,
        carries_attachments=False,
        status=status,
        sender="str@alufluoride.com",
        body_text="We have remitted Rs.1,000.00",
        **kw,
    )
    db_session.add(m)
    db_session.flush()
    return m


def test_email_view_lists_thread_messages(client, db_session, seed_pending):
    email, _ext = seed_pending()
    _msg(db_session, email, 0, "no_content")
    _msg(db_session, email, 1, "new")
    _msg(db_session, email, 2, "seen", seen_reason="fingerprint")
    text = client.get(f"/review/email/{email.id}").text
    assert "Messages in this thread" in text
    assert "internal forward" in text and "seen before" in text


def test_detail_shows_source_message_and_historical_badge(client, db_session, seed_pending):
    email, ext = seed_pending()
    m = _msg(db_session, email, 1, "new")
    ext.email_message_id = m.id
    ext.historical_reason = "earlier_message"
    db_session.flush()
    page = client.get(f"/review/{ext.id}").text
    assert "We have remitted Rs.1,000.00" in page
    assert "Historical: earlier message" in page
    assert f"/review/email/{email.id}" in page


def test_detail_links_duplicate_to_real_detail_route(client, db_session, seed_pending):
    _e1, first = seed_pending()
    _e2, dup = seed_pending()
    dup.duplicate_of_id = first.id
    db_session.flush()
    page = client.get(f"/review/{dup.id}").text
    assert f'href="/review/{first.id}"' in page
    assert "Linked to payment" in page


def test_failed_message_is_listed_and_retryable(client, db_session, seed_pending):
    email, _ext = seed_pending()
    m = _msg(db_session, email, 1, "failed", error_detail="LLMError: boom")
    page = client.get("/review/errors").text
    assert "Failed messages" in page and "boom" in page
    r = client.post(f"/review/messages/{m.id}/retry")
    assert r.status_code == 303
    db_session.refresh(m)
    db_session.refresh(email)
    assert m.status == "new"
    assert m.error_detail is None
    assert email.status == "extracted"


def test_failed_message_listed_even_when_email_done(client, db_session, seed_pending):
    email, _ext = seed_pending()
    email.status = "done"
    _msg(db_session, email, 1, "failed", error_detail="LLMError: still-listed")
    db_session.flush()
    assert "still-listed" in client.get("/review/errors").text


def test_retry_message_rejects_non_failed(client, db_session, seed_pending):
    email, _ext = seed_pending()
    m = _msg(db_session, email, 1, "new")
    r = client.post(f"/review/messages/{m.id}/retry")
    assert r.status_code == 303
    assert "/review/errors?flash=" in r.headers["location"]
    assert "not+failed" in r.headers["location"] or "not%20failed" in r.headers["location"]
    db_session.refresh(m)
    assert m.status == "new"


def test_email_retry_resets_failed_messages(client, db_session, seed_pending):
    email, _ext = seed_pending()
    email.status = "error"
    email.error_detail = "all messages failed"
    failed = _msg(db_session, email, 1, "failed", error_detail="LLMError: boom")
    seen = _msg(db_session, email, 2, "seen", seen_reason="fingerprint")
    db_session.flush()
    r = client.post(f"/review/errors/{email.id}/retry")
    assert r.status_code == 303
    db_session.refresh(failed)
    db_session.refresh(seen)
    assert failed.status == "new" and failed.error_detail is None
    assert seen.status == "seen"


def test_journey_counts_failed_and_seen(client, db_session, seed_pending):
    email, _ext = seed_pending()
    _msg(db_session, email, 1, "failed", error_detail="x")
    _msg(db_session, email, 2, "seen", seen_reason="fingerprint")
    text = client.get("/review/journey-rows").text
    assert "1 message failed" in text and "1 seen before" in text
