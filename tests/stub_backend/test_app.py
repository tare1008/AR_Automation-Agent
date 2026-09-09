from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from stub_backend.app import app
from stub_backend.store import RECEIVED


def _payload():
    return {
        "envelope": {
            "extraction_id": "ext-42",
            "source_email_id": "email-1",
            "vendor_guess": None,
            "extracted_at": datetime(2026, 9, 9, tzinfo=timezone.utc).isoformat(),
            "reviewed_by": "u@co.com",
        },
        "header": {
            "payer_name": "Acme",
            "payer_id": None,
            "payment_reference": "EFT-1",
            "payment_date": "2026-09-05",
            "payment_method": None,
            "currency": "INR",
            "total_paid_amount": "100.00",
        },
        "line_items": [
            {
                "invoice_number": "INV-1",
                "invoice_date": None,
                "invoice_amount": "100.00",
                "discount_taken": None,
                "deduction_amount": None,
                "deduction_reason": None,
                "amount_paid": "100.00",
            }
        ],
    }


@pytest.fixture(autouse=True)
def _clear_store():
    RECEIVED.clear()
    yield
    RECEIVED.clear()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_valid_payload_accepted(client):
    r = await client.post("/remittances", json=_payload())
    assert r.status_code == 201
    assert r.json()["id"] == "ext-42"
    assert "ext-42" in RECEIVED


async def test_invalid_payload_rejected(client):
    bad = _payload()
    del bad["header"]["payment_reference"]
    r = await client.post("/remittances", json=bad)
    assert r.status_code == 422


async def test_idempotency_key_dedupes(client):
    headers = {"Idempotency-Key": "abc-123"}
    r1 = await client.post("/remittances", json=_payload(), headers=headers)
    r2 = await client.post("/remittances", json=_payload(), headers=headers)
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert len(RECEIVED) == 1


async def test_get_returns_stored_payload(client):
    await client.post("/remittances", json=_payload())
    r = await client.get("/remittances/ext-42")
    assert r.status_code == 200
    assert r.json()["header"]["currency"] == "INR"


async def test_get_missing_is_404(client):
    r = await client.get("/remittances/nope")
    assert r.status_code == 404
