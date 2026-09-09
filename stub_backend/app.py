from fastapi import FastAPI, Header, Response

from ar_pipeline.schema.canonical import RemittancePayload
from stub_backend.store import _IDEMPOTENCY, RECEIVED

app = FastAPI(title="AR stub backend")


@app.post("/remittances")
def receive(payload: RemittancePayload, response: Response,
            idempotency_key: str | None = Header(default=None)):
    extraction_id = payload.envelope.extraction_id
    if idempotency_key and idempotency_key in _IDEMPOTENCY:
        response.status_code = 200
        return {"id": _IDEMPOTENCY[idempotency_key], "status": "received"}

    RECEIVED[extraction_id] = payload.model_dump(mode="json")
    if idempotency_key:
        _IDEMPOTENCY[idempotency_key] = extraction_id
    response.status_code = 201
    return {"id": extraction_id, "status": "received"}


@app.get("/remittances/{extraction_id}")
def get_one(extraction_id: str, response: Response):
    if extraction_id not in RECEIVED:
        response.status_code = 404
        return {"detail": "not found"}
    return RECEIVED[extraction_id]
