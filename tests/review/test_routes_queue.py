def test_queue_lists_pending_extractions(client, seed_pending):
    email, ext = seed_pending()
    r = client.get("/review/queue")
    assert r.status_code == 200
    assert email.subject in r.text
    assert f"/review/{ext.id}" in r.text


def test_queue_hides_payment_number_when_email_has_only_one_payment(client, seed_pending):
    seed_pending()
    r = client.get("/review/queue")
    assert "#0" not in r.text


def test_queue_shows_payment_number_when_email_has_multiple_payments(
    client, seed_pending, db_session
):
    from decimal import Decimal

    from ar_pipeline.db.models import Extraction
    from tests.review.conftest import _canonical

    email, ext = seed_pending(canonical=_canonical(payment_index=0))
    second = Extraction(
        email_id=email.id,
        canonical=_canonical(payment_index=1),
        confidence=Decimal("0.7"),
        is_remittance=True,
        validation_flags=[],
        llm_model="claude-opus-5",
        prompt_version="2",
        status="pending_review",
    )
    db_session.add(second)
    db_session.flush()

    r = client.get("/review/queue")
    assert "#0" in r.text
    assert "#1" in r.text


def test_queue_shows_flag_badges(client, seed_pending, db_session):
    email, ext = seed_pending()
    ext.validation_flags = ["totals do not reconcile"]
    db_session.flush()
    r = client.get("/review/queue")
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
