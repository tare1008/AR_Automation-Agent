from decimal import Decimal
from urllib.parse import unquote

from sqlalchemy import select

from ar_pipeline.db.models import ExtractionEdit, Invoice
from ar_pipeline.ledger.matching import number_key


def _invoice(db_session, number, amount="100.00"):
    inv = Invoice(
        invoice_number=number,
        number_key=number_key(number),
        amount=Decimal(amount),
        source="books",
        payer_name="Acme Corp",
    )
    db_session.add(inv)
    db_session.flush()
    return inv


def test_strip_shows_balance_and_after_this(client, db_session, seed_pending):
    _invoice(db_session, "INV-1", "200.00")
    _e, ext = seed_pending()  # line: invoice INV-1, settles 100 (90 paid + 10 TDS)
    text = client.get(f"/review/{ext.id}").text
    assert "your books ₹200.00" in text
    assert "outstanding ₹200.00" in text
    assert "after this payment ₹100.00" in text


def test_near_match_offers_use_button_and_rewrites_the_line(client, db_session, seed_pending):
    inv = _invoice(db_session, "INV-2026-1")
    _e, ext = seed_pending()
    ext.canonical["line_items"] = [
        {**ext.canonical["line_items"][0], "invoice_number": "INV-2026-01"}
    ]
    db_session.flush()
    page = client.get(f"/review/{ext.id}").text
    assert "Use INV-2026-1" in page

    r = client.post(
        f"/review/{ext.id}/use-invoice", data={"line_index": "0", "invoice_id": str(inv.id)}
    )
    assert r.status_code == 303
    assert "Using INV-2026-1" in unquote(r.headers["location"])
    db_session.refresh(ext)
    assert ext.canonical["line_items"][0]["invoice_number"] == "INV-2026-1"
    edits = db_session.scalars(
        select(ExtractionEdit).where(ExtractionEdit.extraction_id == ext.id)
    ).all()
    assert [e.field_path for e in edits] == ["line_items[0].invoice_number"]
    assert not any("did you mean" in f for f in ext.validation_flags)


def test_use_invoice_rejects_a_bad_line_index(client, db_session, seed_pending):
    inv = _invoice(db_session, "INV-2026-1")
    _e, ext = seed_pending()
    r = client.post(
        f"/review/{ext.id}/use-invoice", data={"line_index": "5", "invoice_id": str(inv.id)}
    )
    assert r.status_code == 303
    assert "no such line" in unquote(r.headers["location"])
