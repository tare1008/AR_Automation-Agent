import html
import json
import os
import pathlib

from fastapi import FastAPI, Header, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ar_pipeline.schema.canonical import RemittancePayload
from stub_backend.store import RECEIVED, lookup_idempotency, received_list, record

STATIC_DIR = pathlib.Path(__file__).parent / "static"
# The review app runs as a separate process/port; point back to it for the
# nav link. Override with REVIEW_APP_URL if it's not on the usual dev port.
_REVIEW_APP_URL = os.environ.get("REVIEW_APP_URL", "http://127.0.0.1:8000/review")

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
        f"<td class='truncate' title='{payer_name}'>{payer_name}</td>"
        f"<td class='num'>{currency} {total_paid_amount}</td>"
        f"<td class='ref truncate' title='{payment_reference}'>{payment_reference}</td>"
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


def _fragment_html(rows: list[dict]) -> str:
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
        f"<div class='masthead-row'>{_LEDE}"
        f"<div class='stat'><span class='n'>{len(rows)}</span>"
        "<span class='label'>Received</span></div>"
        "</div>"
        f"{body}"
    )


# Same live-poll pattern as the review app's review.js, sized for this one
# page: refetch the fragment every few seconds, swap it in, pause while the
# tab isn't visible. Inlined rather than a shared static file since this
# stub has no other JS.
_POLL_SCRIPT = """
<script>
(function () {
  var el = document.getElementById('backend-live');
  if (!el) return;
  setInterval(function () {
    if (document.hidden) return;
    fetch('/fragment').then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) { if (html !== null) el.innerHTML = html; })
      .catch(function () {});
  }, 3500);
})();
</script>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    inner = _fragment_html(received_list())
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>AR Backend by MLDeep Systems</title>"
        "<link rel='stylesheet' href='/static/backend.css'>"
        "</head><body>"
        "<header>"
        f"<nav class='topnav'><a href='{_REVIEW_APP_URL}'>Review app</a></nav>"
        "<div class='brand-text'>"
        "<span class='brand-word'>AR Backend</span>"
        "<span class='brand-by'>by MLDeep Systems</span>"
        "</div>"
        "</header>"
        f"<main><div id='backend-live'>{inner}</div></main>"
        f"{_POLL_SCRIPT}"
        "</body></html>"
    )


@app.get("/fragment", response_class=HTMLResponse)
def fragment() -> str:
    return _fragment_html(received_list())


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
