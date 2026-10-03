from __future__ import annotations

import datetime as dt

import ar_pipeline.config as config_module
from ar_pipeline.db.models import Email, EmailMessage, Extraction
from ar_pipeline.threads.dedupe import apply_history, assign_payment_key


def _email(db_session, mid):
    e = Email(
        internet_message_id=mid,
        sender_address="a@b.com",
        sender_domain="b.com",
        subject="s",
        received_at=dt.datetime(2026, 2, 18, tzinfo=dt.UTC),
        status="review",
    )
    db_session.add(e)
    db_session.flush()
    return e


def _row(
    db_session,
    email,
    *,
    ref="HDFC1234567890",
    rtype="utr",
    total="100.00",
    status="pending_review",
    date="2026-02-18",
    payer="Acme Corp",
):
    x = Extraction(
        email_id=email.id,
        status=status,
        is_remittance=True,
        validation_flags=[],
        canonical={
            "header": {
                "payment_reference": ref,
                "payment_reference_type": rtype,
                "payer_name": payer,
                "total_paid_amount": total,
                "payment_date": date,
            },
            "line_items": [{}],
        },
    )
    db_session.add(x)
    db_session.flush()
    return x


def test_first_strong_key_is_assigned(db_session):
    x = _row(db_session, _email(db_session, "d1"))
    assign_payment_key(db_session, x)
    assert (x.payment_key, x.payment_key_strength) == ("utr:HDFC1234567890", "strong")


def test_same_reference_and_amount_within_one_rupee_is_a_duplicate(db_session):
    a = _row(db_session, _email(db_session, "d2"), total="1000.00")
    assign_payment_key(db_session, a)
    b = _row(db_session, _email(db_session, "d3"), total="1000.49")
    assign_payment_key(db_session, b)
    assert b.status == "duplicate" and b.duplicate_of_id == a.id and b.payment_key is None


def test_same_reference_different_amount_is_flagged_not_dropped(db_session):
    a = _row(db_session, _email(db_session, "d4"), total="1000.00")
    assign_payment_key(db_session, a)
    b = _row(db_session, _email(db_session, "d5"), total="2000.00")
    assign_payment_key(db_session, b)
    assert b.status == "pending_review" and b.payment_key is None and b.duplicate_of_id == a.id
    assert any("was already used for" in f for f in b.validation_flags)


def test_rejected_earlier_is_noted(db_session):
    a = _row(db_session, _email(db_session, "d6"))
    assign_payment_key(db_session, a)
    a.status, a.reviewed_by, a.reject_reason = "rejected", "Asha", "wrong payer"
    a.reviewed_at = dt.datetime(2026, 2, 19, tzinfo=dt.UTC)
    db_session.flush()
    b = _row(db_session, _email(db_session, "d7"))
    assign_payment_key(db_session, b)
    assert b.payment_key == "utr:HDFC1234567890"
    assert any(
        f.startswith("header: rejected before on 19 Feb 2026 by Asha") for f in b.validation_flags
    )


def test_weak_keys_flag_but_never_block(db_session):
    a = _row(db_session, _email(db_session, "d8"), ref=None, rtype=None)
    assign_payment_key(db_session, a)
    b = _row(db_session, _email(db_session, "d9"), ref=None, rtype=None)
    assign_payment_key(db_session, b)
    assert b.status == "pending_review" and b.payment_key_strength == "weak"
    assert any("possible duplicate of payment" in f for f in b.validation_flags)


def test_different_references_never_block_and_rerun_on_holder_is_noop(db_session):
    a = _row(db_session, _email(db_session, "d10"), ref="HDFC1111111111")
    b = _row(db_session, _email(db_session, "d11"), ref="HDFC2222222222")
    assign_payment_key(db_session, a)
    assign_payment_key(db_session, b)
    assert a.payment_key == "utr:HDFC1111111111" and b.payment_key == "utr:HDFC2222222222"
    assert a.status == b.status == "pending_review"
    assert a.duplicate_of_id is None and b.duplicate_of_id is None
    assign_payment_key(db_session, a)
    assert a.payment_key == "utr:HDFC1111111111"
    assert a.status == "pending_review" and a.duplicate_of_id is None
    assert a.validation_flags == []


def test_history_earlier_message(db_session):
    e = _email(db_session, "h1")
    m = EmailMessage(
        email_id=e.id,
        position=2,
        raw_header="",
        is_internal=False,
        carries_attachments=False,
        status="new",
    )
    db_session.add(m)
    x = _row(db_session, e)
    apply_history(db_session, x, m, newest_content_position=1)
    assert x.historical_reason == "earlier_message"
    assert x.validation_flags[-1].startswith("header: historical — earlier message")


def test_history_before_go_live(db_session, monkeypatch):
    monkeypatch.setenv("GO_LIVE_DATE", "2026-03-01")
    config_module.get_settings.cache_clear()
    try:
        x = _row(db_session, _email(db_session, "h2"), date="2026-02-18")
        apply_history(db_session, x, None, newest_content_position=None)
        assert x.historical_reason == "before_go_live"
        assert "before go-live 01 Mar 2026" in x.validation_flags[-1]
    finally:
        config_module.get_settings.cache_clear()


def test_newest_message_after_go_live_is_not_historical(db_session):
    x = _row(db_session, _email(db_session, "h3"))
    apply_history(db_session, x, None, newest_content_position=None)
    assert x.historical_reason is None and x.validation_flags == []
