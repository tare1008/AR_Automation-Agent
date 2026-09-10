def test_queue_lists_pending_extractions(client, seed_pending):
    email, ext = seed_pending()
    r = client.get("/review")
    assert r.status_code == 200
    assert email.subject in r.text
    assert f"/review/{ext.id}" in r.text


def test_queue_shows_flag_badges(client, seed_pending, db_session):
    email, ext = seed_pending()
    ext.validation_flags = ["totals do not reconcile"]
    db_session.flush()
    r = client.get("/review")
    assert r.status_code == 200
    assert "totals do not reconcile" in r.text


def test_errors_page_lists_errored_emails_and_retry_works(client, seed_pending, db_session):
    email, ext = seed_pending()
    email.status = "error"
    email.error_detail = "kaboom"
    db_session.flush()

    r = client.get("/review/errors")
    assert r.status_code == 200
    assert "kaboom" in r.text

    r2 = client.post(f"/review/errors/{email.id}/retry")
    assert r2.status_code == 303

    r3 = client.get("/review/errors")
    assert "kaboom" not in r3.text
