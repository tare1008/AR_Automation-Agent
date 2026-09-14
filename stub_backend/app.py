import html
import json
import pathlib

from fastapi import FastAPI, Header, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ar_pipeline.schema.canonical import RemittancePayload
from stub_backend.store import RECEIVED, lookup_idempotency, received_list, record

STATIC_DIR = pathlib.Path(__file__).parent / "static"

app = FastAPI(title="AR stub backend")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _row_html(payload: dict) -> str:
    envelope = payload.get("envelope", {})
    header = payload.get("header", {})
    line_items = payload.get("line_items", [])
    extraction_id = html.escape(str(envelope.get("extraction_id", "")))
    payer_name = html.escape(str(header.get("payer_name") or "—"))
    total_paid_amount = html.escape(str(header.get("total_paid_amount") or "—"))
    currency = html.escape(str(header.get("currency") or ""))
    payment_reference = html.escape(str(header.get("payment_reference") or "—"))
    line_item_count = html.escape(str(len(line_items)))
    raw_json = html.escape(json.dumps(payload, indent=2))
    return (
        f"<tr><td class='id' title='{extraction_id}'>{extraction_id[:8]}&hellip;</td>"
        f"<td>{payer_name}</td>"
        f"<td class='num'>{currency} {total_paid_amount}</td>"
        f"<td class='ref'>{payment_reference}</td>"
        f"<td class='num'>{line_item_count}</td>"
        f"<td><details><summary>View JSON</summary>"
        f"<pre>{raw_json}</pre></details></td></tr>"
    )


_EMPTY = (
    "<p class='empty'>No remittances received yet. Auto-approved and "
    "human-approved payments land here once the pipeline delivers them.</p>"
)
_LEDE = (
    "<p class='lede'>This mirrors what a production backend would receive "
    "at <code>POST /remittances</code>: the same canonical JSON the "
    "pipeline builds from each approved email, whether it was approved "
    "automatically or by a reviewer.</p>"
)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    rows = received_list()
    table = "".join(_row_html(payload) for payload in rows)
    body = (
        "<div class='table-wrap'><table><thead><tr>"
        "<th>Extraction</th><th>Payer</th><th>Amount</th><th>Reference</th>"
        "<th>Lines</th><th>Payload</th>"
        f"</tr></thead><tbody>{table}</tbody></table></div>"
        if rows
        else _EMPTY
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>AR stub backend</title>"
        "<link rel='stylesheet' href='/static/backend.css'>"
        "</head><body>"
        "<header><span class='mark'>B</span><div>"
        "<h1>AR stub backend</h1>"
        "<p>Stand-in for the client's real system &mdash; every delivered "
        "remittance lands here.</p>"
        "</div></header>"
        f"<main>{_LEDE}"
        f"<div class='stat'><span class='n'>{len(rows)}</span>"
        "<span class='label'>Received</span></div>"
        f"{body}"
        "</main></body></html>"
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
    if idempotency_key:
        existing = lookup_idempotency(idempotency_key)
        if existing is not None:
            response.status_code = 200
            return {"id": existing, "status": "received"}

    record(extraction_id, payload.model_dump(mode="json"), idempotency_key)
    response.status_code = 201
    return {"id": extraction_id, "status": "received"}


@app.get("/remittances/{extraction_id}")
def get_one(extraction_id: str, response: Response):
    if extraction_id not in RECEIVED:
        response.status_code = 404
        return {"detail": "not found"}
    return RECEIVED[extraction_id]
