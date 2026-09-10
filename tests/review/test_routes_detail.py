import uuid

from fastapi.testclient import TestClient

from ar_pipeline.main import app


def test_detail_renders_form_fields_from_canonical(client, seed_pending):
    email, ext = seed_pending()
    r = client.get(f"/review/{ext.id}")
    assert r.status_code == 200
    assert 'name="header.payer_name"' in r.text
    assert "Acme Corp" in r.text
    assert 'name="line_items[0].invoice_number"' in r.text
    assert 'name="line_items[0].deductions[0].type"' in r.text


def test_detail_sanitises_email_body(client, seed_pending):
    email, ext = seed_pending()
    email.body_html = "<p>hello</p><script>alert(1)</script>"
    r = client.get(f"/review/{ext.id}")
    assert "alert(1)" not in r.text  # nh3 stripped the <script>


def test_detail_unknown_id_404(client):
    r = client.get(f"/review/{uuid.uuid4()}")
    assert r.status_code == 404


def test_detail_requires_login():
    with TestClient(app, follow_redirects=False) as anon:
        r = anon.get(f"/review/{uuid.uuid4()}")
        assert r.status_code == 303


def test_attachment_stream_rejects_foreign_attachment(client, seed_pending, db_session):
    email, ext = seed_pending()
    r = client.get(f"/review/{ext.id}/attachment/{uuid.uuid4()}")
    assert r.status_code == 404
