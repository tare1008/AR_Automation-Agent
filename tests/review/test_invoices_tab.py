from decimal import Decimal

from ar_pipeline.db.models import Invoice
from ar_pipeline.ledger.matching import number_key


def _invoice(db_session, number="MST-2026-7550", amount="180000", source="books") -> Invoice:
    inv = Invoice(
        invoice_number=number,
        number_key=number_key(number),
        amount=Decimal(amount),
        source=source,
        payer_name="Orion Fabricators Pvt Ltd",
    )
    db_session.add(inv)
    db_session.flush()
    return inv


def test_nav_has_invoices(client):
    assert 'href="/review/invoices"' in client.get("/review").text


def test_invoices_page_lists_invoices_with_indian_grouping(client, db_session):
    _invoice(db_session)
    page = client.get("/review/invoices")
    assert page.status_code == 200
    assert "MST-2026-7550" in page.text
    assert "₹1,80,000.00" in page.text
    assert "your books" in page.text
    assert 'data-poll-url="/review/invoices-rows' in page.text


def test_unverified_badge(client, db_session):
    _invoice(db_session, source="email")
    assert "unverified" in client.get("/review/invoices-rows").text


def test_status_filter(client, db_session):
    _invoice(db_session, "A-1")
    text = client.get("/review/invoices?status=paid").text
    assert "A-1" not in text


def test_upload_imports_and_reports_skips(client, db_session):
    csv = b"invoice_number,invoice_amount\nA-1,100\nB-2,abc\n"
    r = client.post("/review/invoices/import", files={"file": ("open.csv", csv, "text/csv")})
    assert r.status_code == 200
    assert "Imported 1" in r.text
    assert "1 row skipped" in r.text
    assert "row 3: invoice_amount is not a number" in r.text


def test_upload_whole_file_error(client):
    r = client.post("/review/invoices/import", files={"file": ("x.csv", b"foo\n1\n", "text/csv")})
    assert r.status_code == 200
    assert "missing column" in r.text


def test_template_download(client):
    r = client.get("/review/invoices/template.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert r.text.startswith("invoice_number,payer_name,invoice_date,invoice_amount")


def test_detail_page(client, db_session):
    inv = _invoice(db_session)
    r = client.get(f"/review/invoices/{inv.id}")
    assert r.status_code == 200
    assert "MST-2026-7550" in r.text


def test_detail_404(client):
    assert client.get("/review/invoices/00000000-0000-0000-0000-000000000000").status_code == 404


def test_requires_login():
    from fastapi.testclient import TestClient

    from ar_pipeline.main import app

    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get("/review/invoices").status_code in (303, 401)


def test_upload_rechecks_pending_items(client, db_session, seed_pending):
    _, ext = seed_pending()
    ext.validation_flags = ["line 0: INV-1 isn't in your open invoices"]
    db_session.flush()
    number = ext.canonical["line_items"][0]["invoice_number"]
    amount = ext.canonical["line_items"][0]["invoice_amount"]
    csv = f"invoice_number,payer_name,invoice_amount\n{number},Acme Corp,{amount}\n".encode()
    r = client.post("/review/invoices/import", files={"file": ("open.csv", csv, "text/csv")})
    assert r.status_code == 200
    db_session.refresh(ext)
    assert ext.validation_flags == []
