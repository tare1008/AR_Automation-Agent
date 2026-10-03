from __future__ import annotations

ADJ = {
    "invoice_number": "251000458DISCO",
    "invoice_date": None,
    "invoice_amount": "0",
    "deductions": [{"type": "discount", "amount": "5.00", "reason": None}],
    "amount_paid": "-5.00",
    "kind": "adjustment",
    "applies_to": None,
}


def _with_adjustment(seed_pending, db_session, read_info=None):
    email, ext = seed_pending()
    canonical = dict(ext.canonical)
    first = {**canonical["line_items"][0], "invoice_number": "CBB2510004583"}
    canonical["line_items"] = [first, ADJ]
    canonical["header"] = {**canonical["header"], "total_paid_amount": "85.00"}
    ext.canonical = canonical
    ext.read_info = read_info
    db_session.flush()
    return email, ext


def test_detail_shows_adjustment_strip_and_use_button(client, db_session, seed_pending):
    _email, ext = _with_adjustment(seed_pending, db_session)
    page = client.get(f"/review/{ext.id}").text
    assert "Adjustment" in page and "Which invoice does it reduce?" in page
    assert "Use CBB2510004583" in page
    assert 'name="line_items[1].kind" value="adjustment"' in page


def test_use_adjustment_sets_applies_to(client, db_session, seed_pending):
    _email, ext = _with_adjustment(seed_pending, db_session)
    r = client.post(
        f"/review/{ext.id}/use-adjustment",
        data={"line_index": "1", "number": "CBB2510004583"},
    )
    assert r.status_code == 303 and "Adjustment%20now%20reduces" in r.headers["location"]
    db_session.refresh(ext)
    assert ext.canonical["line_items"][1]["applies_to"] == "CBB2510004583"


def test_use_adjustment_refuses_a_number_that_is_not_suggested(client, db_session, seed_pending):
    _email, ext = _with_adjustment(seed_pending, db_session)
    client.post(f"/review/{ext.id}/use-adjustment", data={"line_index": "1", "number": "X1"})
    db_session.refresh(ext)
    assert ext.canonical["line_items"][1]["applies_to"] is None


def test_totals_badges(client, db_session, seed_pending):
    _email, ext = _with_adjustment(
        seed_pending,
        db_session,
        read_info={"path": "table", "mapping": "learned", "document_totals": {"words": "85.00"}},
    )
    page = client.get(f"/review/{ext.id}").text
    assert "Totals match the document" in page and "Rows read from the table" in page
    ext.read_info = {"path": "ai", "mapping": None, "document_totals": {"words": "999.00"}}
    db_session.flush()
    assert "Totals don't match the document" in client.get(f"/review/{ext.id}").text


def test_saving_an_old_payload_records_no_format_noise(client, db_session, seed_pending):
    from sqlalchemy import select

    from ar_pipeline.db.models import ExtractionEdit

    _email, probe = seed_pending()
    legacy = dict(probe.canonical)
    legacy["envelope"] = {
        k: v for k, v in legacy.get("envelope", {}).items() if k != "schema_version"
    }
    legacy["line_items"] = [
        {k: v for k, v in li.items() if k not in ("kind", "applies_to")}
        for li in legacy["line_items"]
    ]
    _email, ext = seed_pending(canonical=legacy)
    assert "kind" not in ext.canonical["line_items"][0]
    form = {
        "header.payer_name": "Acme Corporation",  # one real change
        "header.total_paid_amount": "90.00",
        "header.currency": "INR",
        "header.payment_reference": "UTR-1",
        "header.payment_reference_type": "utr",
        "header.payment_date": "2026-09-05",
        "header.payment_method": "RTGS",
        "line_items[0].invoice_number": "INV-1",
        "line_items[0].invoice_date": "2026-08-01",
        "line_items[0].invoice_amount": "100.00",
        "line_items[0].amount_paid": "90.00",
        "line_items[0].kind": "invoice",
        "line_items[0].deductions[0].type": "tds",
        "line_items[0].deductions[0].amount": "10.00",
        "line_items[0].deductions[0].reason": "194Q",
    }
    r = client.post(f"/review/{ext.id}/edit", data=form)
    assert r.status_code == 303
    paths = list(
        db_session.scalars(
            select(ExtractionEdit.field_path).where(ExtractionEdit.extraction_id == ext.id)
        )
    )
    assert not any(
        p.endswith(".kind") or p.endswith(".applies_to") or "schema_version" in p for p in paths
    )
    assert "header.payer_name" in paths


def _adjustment_line(seed_pending, db_session, **changes):
    email, ext = _with_adjustment(seed_pending, db_session)
    canonical = dict(ext.canonical)
    canonical["line_items"] = [canonical["line_items"][0], {**ADJ, **changes}]
    ext.canonical = canonical
    db_session.flush()
    return ext


def test_strip_says_new_invoice_only_for_a_line_of_this_payment(client, db_session, seed_pending):
    # R9.3
    ext = _adjustment_line(seed_pending, db_session, applies_to="CBB2510004583")
    page = client.get(f"/review/{ext.id}").text
    assert "Reduces CBB2510004583 &mdash; a new invoice in this payment." in page

    ext = _adjustment_line(seed_pending, db_session, applies_to="ZZZ9999999")
    page = client.get(f"/review/{ext.id}").text
    assert "Reduces ZZZ9999999 &mdash; not in this payment or your invoices." in page
    assert "a new invoice in this payment" not in page


def test_unconfirmed_exact_target_can_be_used(client, db_session, seed_pending):
    # R9.1: exact same-payment match left unconfirmed (applies_to empty)
    ext = _adjustment_line(seed_pending, db_session, invoice_number="2510004583DISCO")
    assert "Use CBB2510004583" in client.get(f"/review/{ext.id}").text
    r = client.post(
        f"/review/{ext.id}/use-adjustment",
        data={"line_index": "1", "number": "CBB2510004583"},
    )
    assert r.status_code == 303 and "Adjustment%20now%20reduces" in r.headers["location"]
    db_session.refresh(ext)
    assert ext.canonical["line_items"][1]["applies_to"] == "CBB2510004583"
