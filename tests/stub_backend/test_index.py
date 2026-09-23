from fastapi.testclient import TestClient

from stub_backend.app import app
from stub_backend.store import _IDEMPOTENCY, RECEIVED


def _payload(eid="ext-1"):
    return {
        "envelope": {
            "extraction_id": eid,
            "source_email_id": "e",
            "payment_index": 0,
            "vendor_guess": None,
            "extracted_at": "2026-09-01T00:00:00Z",
            "reviewed_by": "auto",
        },
        "header": {
            "payer_name": "Acme",
            "payer_id": None,
            "payment_reference": "UTR9",
            "payment_reference_type": "utr",
            "payment_date": None,
            "payment_method": None,
            "currency": "INR",
            "total_paid_amount": "100.00",
            "deductions": [],
        },
        "line_items": [
            {
                "invoice_number": "INV-1",
                "invoice_date": None,
                "invoice_amount": "100.00",
                "deductions": [],
                "amount_paid": "100.00",
            }
        ],
    }


def _client():
    RECEIVED.clear()
    _IDEMPOTENCY.clear()
    return TestClient(app)


def test_remittances_list_json():
    c = _client()
    c.post("/remittances", json=_payload("ext-1"), headers={"Idempotency-Key": "ext-1"})
    r = c.get("/remittances")
    assert r.json()["count"] == 1
    assert r.json()["remittances"][0]["envelope"]["extraction_id"] == "ext-1"


def test_index_html_lists_received():
    c = _client()
    c.post("/remittances", json=_payload("ext-42"), headers={"Idempotency-Key": "ext-42"})
    r = c.get("/")
    assert r.status_code == 200
    assert "ext-42" in r.text and "Acme" in r.text


def test_index_escapes_values():
    c = _client()
    p = _payload("ext-x")
    p["header"]["payer_name"] = "<script>alert(1)</script>"
    c.post("/remittances", json=p, headers={"Idempotency-Key": "ext-x"})
    assert "<script>alert(1)</script>" not in c.get("/").text


def test_fragment_is_bare_and_matches_index_content():
    c = _client()
    c.post("/remittances", json=_payload("ext-77"), headers={"Idempotency-Key": "ext-77"})
    r = c.get("/fragment")
    assert r.status_code == 200
    assert "<!doctype" not in r.text.lower()
    assert "<header" not in r.text.lower()
    assert "ext-77" in r.text


def test_fragment_shows_empty_state_with_no_remittances():
    c = _client()
    assert "No remittances received yet" in c.get("/fragment").text
