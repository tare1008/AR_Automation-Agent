from __future__ import annotations

from sqlalchemy import select

from ar_pipeline.db.models import Delivery, Extraction, ExtractionEdit, InvoicePayment


def _historical(db_session, seed_pending):
    _e, ext = seed_pending()
    ext.historical_reason = "earlier_message"
    ext.validation_flags = [
        "header: historical — earlier message in this thread; check it isn't already in your books"
    ]
    db_session.flush()
    return ext


def _loc(r):
    return r.headers["location"].replace("%20", " ")


def test_bulk_mark_already_recorded_posts_to_ledger_without_delivery(
    client, db_session, seed_pending
):
    ext = _historical(db_session, seed_pending)
    _e2, current = seed_pending()  # not historical: must be ignored
    r = client.post(
        "/review/bulk-already-recorded",
        data={"extraction_id": [str(ext.id), str(current.id)]},
    )
    assert r.status_code == 303 and "1 marked already recorded" in _loc(r)
    db_session.expire_all()
    assert db_session.get(Extraction, ext.id).status == "already_recorded"
    assert db_session.get(Extraction, current.id).status == "pending_review"
    assert not db_session.scalars(select(Delivery).where(Delivery.extraction_id == ext.id)).all()
    assert db_session.scalars(
        select(InvoicePayment).where(InvoicePayment.extraction_id == ext.id)
    ).all()


def test_queue_offers_already_recorded_only_for_historical(client, db_session, seed_pending):
    ext = _historical(db_session, seed_pending)
    _e2, current = seed_pending()
    text = client.get("/review/queue").text
    assert "/review/bulk-already-recorded" in text
    assert f'value="{ext.id}" form="bulkRecordedForm"' in text
    assert f'value="{current.id}" form="bulkRecordedForm"' not in text


def test_detail_button_for_historical(client, db_session, seed_pending):
    ext = _historical(db_session, seed_pending)
    page = client.get(f"/review/{ext.id}").text
    assert f"/review/{ext.id}/already-recorded" in page
    r = client.post(f"/review/{ext.id}/already-recorded")
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Extraction, ext.id).status == "already_recorded"


def _form(ref="UTR1"):
    return {
        "header.payer_name": "Acme Corp",
        "header.currency": "INR",
        "header.total_paid_amount": "90.00",
        "header.payment_reference": ref,
        "header.payment_reference_type": "utr",
        "line_items[0].invoice_number": "INV-1",
        "line_items[0].invoice_amount": "100.00",
        "line_items[0].amount_paid": "90.00",
        "line_items[0].deductions[0].type": "tds",
        "line_items[0].deductions[0].amount": "10.00",
    }


def test_editing_a_reference_onto_an_existing_payment_is_refused(client, db_session, seed_pending):
    _e1, first = seed_pending()
    first.payment_key, first.payment_key_strength, first.status = "utr:UTR1", "strong", "approved"
    _e2, second = seed_pending()
    db_session.flush()
    before = dict(second.canonical)
    r = client.post(f"/review/{second.id}/edit", data=_form())
    assert "already belongs to payment" in _loc(r)
    db_session.expire_all()
    row = db_session.get(Extraction, second.id)
    assert row.status == "pending_review"
    assert row.canonical == before
    assert row.duplicate_of_id is None
    assert not db_session.scalars(
        select(ExtractionEdit).where(ExtractionEdit.extraction_id == second.id)
    ).all()


def test_editing_a_reference_to_a_free_one_rekeys_the_row(client, db_session, seed_pending):
    _e, ext = seed_pending()
    ext.payment_key, ext.payment_key_strength = "utr:OLD", "strong"
    db_session.flush()
    r = client.post(f"/review/{ext.id}/edit", data=_form("UTR9"))
    assert r.status_code == 303 and "flash=Saved" in r.headers["location"]
    db_session.expire_all()
    row = db_session.get(Extraction, ext.id)
    assert row.status == "pending_review"
    assert row.payment_key == "utr:UTR9" and row.payment_key_strength == "strong"


def test_bulk_ignores_approved_non_remittance_and_empty_canonical(client, db_session, seed_pending):
    approved = _historical(db_session, seed_pending)
    approved.status = "approved"
    _e, junk = seed_pending(is_remittance=False)
    junk.historical_reason = "earlier_message"
    _e, empty = seed_pending(canonical={})
    empty.historical_reason = "earlier_message"
    db_session.flush()
    r = client.post(
        "/review/bulk-already-recorded",
        data={"extraction_id": [str(approved.id), str(junk.id), str(empty.id)]},
    )
    assert "0 marked already recorded" in _loc(r)
    db_session.expire_all()
    assert db_session.get(Extraction, approved.id).status == "approved"
    assert db_session.get(Extraction, junk.id).status == "pending_review"
    assert db_session.get(Extraction, empty.id).status == "pending_review"


def test_edit_keeps_historical_and_truncation_flags(client, db_session, seed_pending):
    from ar_pipeline.normalize.service import TRUNCATED_FLAG

    ext = _historical(db_session, seed_pending)
    hist = ext.validation_flags[0]
    ext.validation_flags = [hist, TRUNCATED_FLAG]
    db_session.flush()
    form = _form("UTR-1")
    form["header.payer_name"] = "Acme Corporation"
    client.post(f"/review/{ext.id}/edit", data=form)
    db_session.expire_all()
    flags = db_session.get(Extraction, ext.id).validation_flags
    assert flags[:2] == [hist, TRUNCATED_FLAG]


def test_edit_keeps_reference_flag_when_key_untouched_and_approve_not_blocked(
    client, db_session, seed_pending
):
    _e, ext = seed_pending()
    flag = "header: reference UTR1 was already used for ₹500.00 (payment x)"
    ext.validation_flags = [flag]
    db_session.flush()
    form = _form("UTR-1")  # same reference as the seed
    form["header.payment_date"] = "2026-09-05"
    client.post(f"/review/{ext.id}/edit", data=form)
    db_session.expire_all()
    row = db_session.get(Extraction, ext.id)
    assert flag in row.validation_flags
    form["approve"] = "1"
    client.post(f"/review/{ext.id}/edit", data=form)
    db_session.expire_all()
    assert db_session.get(Extraction, ext.id).status == "approved"
