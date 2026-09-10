from __future__ import annotations

from sqlalchemy import select

from ar_pipeline.db.models import Delivery, ExtractionEdit


def _form(**over: str) -> dict[str, str]:
    base = {
        "header.payer_name": "Acme Corp",
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
        "approve": "0",
    }
    base.update(over)
    return base


def test_edit_writes_audit_and_keeps_pending(client, seed_pending, db_session):
    email, ext = seed_pending()
    r = client.post(
        f"/review/{ext.id}/edit",
        data=_form(**{"header.payer_name": "Acme Corporation"}),
    )
    assert r.status_code == 303
    rows = db_session.scalars(
        select(ExtractionEdit).where(ExtractionEdit.extraction_id == ext.id)
    ).all()
    assert [x.field_path for x in rows] == ["header.payer_name"]
    db_session.refresh(ext)
    assert ext.status == "pending_review"


def test_edit_then_approve_inserts_delivery(client, seed_pending, db_session):
    email, ext = seed_pending()
    r = client.post(f"/review/{ext.id}/edit", data=_form(approve="1"))
    assert r.status_code == 303
    assert r.headers["location"] == "/review?flash=Approved"
    db_session.refresh(ext)
    assert ext.status == "approved"
    assert db_session.scalars(select(Delivery).where(Delivery.extraction_id == ext.id)).all()


def test_edit_invalid_redirects_with_flash_and_no_change(client, seed_pending, db_session):
    email, ext = seed_pending()
    r = client.post(
        f"/review/{ext.id}/edit",
        data=_form(**{"line_items[0].invoice_amount": "not-a-number"}),
    )
    assert r.status_code == 303
    assert "flash=" in r.headers["location"]
    db_session.refresh(ext)
    assert ext.status == "pending_review"
    assert (
        db_session.scalars(
            select(ExtractionEdit).where(ExtractionEdit.extraction_id == ext.id)
        ).all()
        == []
    )


def test_reject_route(client, seed_pending, db_session):
    email, ext = seed_pending()
    r = client.post(f"/review/{ext.id}/reject", data={"reason": "dup"})
    assert r.status_code == 303
    db_session.refresh(ext)
    assert ext.status == "rejected" and ext.reject_reason == "dup"


def test_reprocess_route(client, seed_pending, db_session):
    email, ext = seed_pending()
    r = client.post(f"/review/{ext.id}/reprocess")
    assert r.status_code == 303
    db_session.refresh(ext)
    db_session.refresh(email)
    assert ext.status == "superseded" and email.status == "classified"
