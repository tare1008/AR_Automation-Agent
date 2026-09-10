import html
import json

from fastapi import FastAPI, Header, Response
from fastapi.responses import HTMLResponse

from ar_pipeline.schema.canonical import RemittancePayload
from stub_backend.store import _IDEMPOTENCY, RECEIVED, received_list

app = FastAPI(title="AR stub backend")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    rows = received_list()
    body_rows = []
    for payload in rows:
        envelope = payload.get("envelope", {})
        header = payload.get("header", {})
        line_items = payload.get("line_items", [])
        extraction_id = html.escape(str(envelope.get("extraction_id", "")))
        payer_name = html.escape(str(header.get("payer_name", "")))
        total_paid_amount = html.escape(str(header.get("total_paid_amount", "")))
        payment_reference = html.escape(str(header.get("payment_reference", "")))
        line_item_count = html.escape(str(len(line_items)))
        raw_json = html.escape(json.dumps(payload, indent=2))
        body_rows.append(
            f"<tr><td>{extraction_id}</td><td>{payer_name}</td>"
            f"<td>{total_paid_amount}</td><td>{payment_reference}</td>"
            f"<td>{line_item_count}</td>"
            f"<td><details><summary>JSON</summary><pre>{raw_json}</pre></details></td></tr>"
        )
    table = "".join(body_rows)
    return (
        "<html><head><title>AR stub backend</title></head><body>"
        f"<h1>AR stub backend &mdash; received remittances ({len(rows)})</h1>"
        '<table border="1"><thead><tr>'
        "<th>extraction_id</th><th>payer_name</th><th>total_paid_amount</th>"
        "<th>payment_reference</th><th>#line_items</th><th>raw</th>"
        f"</tr></thead><tbody>{table}</tbody></table>"
        "</body></html>"
    )


@app.get("/remittances")
def list_remittances() -> dict:
    return {"count": len(RECEIVED), "remittances": received_list()}


@app.post("/remittances")
def receive(
    payload: RemittancePayload,
    response: Response,
    idempotency_key: str | None = Header(default=None),
):
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
