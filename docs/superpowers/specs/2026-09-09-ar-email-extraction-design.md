# AR Email Settlement Extraction Pipeline — Design

**Date:** 2026-09-09
**Status:** Approved (brainstorming); pending spec review

## Problem

A shared mailbox receives accounts-receivable settlement / remittance
advice from many vendors. Vendors send the same information in
different forms:

- an image (screenshot) of the settlement details
- an Excel attachment
- a PDF attachment (native text, or scanned)
- a table typed directly into the email body

The goal: extract the settlement data regardless of form, normalize it
into one canonical structure, let a human review and correct it, then
POST it to a backend REST API. What the backend does afterward (feeding
the Oracle ERP) is out of scope.

## Scope

**In scope:** inbox → classify → extract → normalize → human review →
POST to backend. Plus a stub backend that validates the canonical
payload, so the pipeline is testable end-to-end now.

**Out of scope:** the real backend, the Oracle ERP integration,
anything downstream of the POST.

**Phasing:**

- **Phase 1 (this spec):** every extraction is human-reviewed before
  it is sent.
- **Phase 2 (later):** confidence-based auto-send; high-confidence,
  flag-free extractions skip review. Designed for but not built now.

## Decisions (from brainstorming)

| Topic | Decision |
|---|---|
| Email source | Poll a single shared mailbox via Microsoft Graph API (M365) |
| Graph auth | App-only (client credentials) via MSAL; `Mail.Read` application permission, scoped to the shared mailbox |
| Backend | REST API we POST JSON to. We design the canonical schema now and build a stub endpoint; hand the schema to the backend team later |
| Canonical data | Header (payer, payment ref, date, method, currency, total) + line items (invoice no/date/amount, discount, deduction + reason, amount paid). Not frozen — refined against real samples during implementation |
| Currency | Default **INR**; field stored as ISO-4217 for future flexibility |
| Extraction strategy | Deterministic parsers where possible (openpyxl for Excel, `pandas.read_html` for body tables, pdfplumber for text PDFs); LLM vision only for images and scanned PDFs. Normalization into the canonical schema is done by an LLM, not per-vendor config |
| Review | Phase 1: human-in-the-loop web UI for every extraction |
| Volume | ~50 emails/day, small AR team |
| App auth | Internet-facing, real auth; provider is a swappable interface (slot for Entra ID SSO), simple provider to start |
| LLM | Swappable `LLMClient` interface; start with the Anthropic API for development |
| Vendor handling | Optional per-vendor hints table; unknown senders still flow through with best-effort extraction into review |
| Infrastructure | Cloud (leaning Azure, not finalized); one app container + managed Postgres + blob storage; Docker |
| Architecture | Single sequential FastAPI service with a DB-`status`-column queue walked by one background loop. No Redis/workers in Phase 1 |

## Architecture

One FastAPI application, one Postgres database, one blob store for
attachments. Seven modules, each with a narrow interface, advanced by a
single background loop (`worker.py`, APScheduler in-process).

```
                 ┌────────────────────────────────────────────┐
   Graph API ───▶│ ingest/   poll shared inbox (delta query)   │
 (shared inbox)  │           store email + attachments → blob  │
                 └────────────────────────────────────────────┘
                                  │  email.status = new
                                  ▼
                 ┌────────────────────────────────────────────┐
                 │ classify/ email → list of sources:         │
                 │   body_table | excel | pdf_text |          │
                 │   pdf_scanned | image   (skip logos)       │
                 └────────────────────────────────────────────┘
                                  ▼
                 ┌────────────────────────────────────────────┐
                 │ extract/  one extractor per kind, same     │
                 │   interface → RawExtraction{tables,text,   │
                 │   images}                                  │
                 └────────────────────────────────────────────┘
                                  ▼
                 ┌────────────────────────────────────────────┐
                 │ normalize/ LLM maps raw → canonical schema │
                 │   + per-field confidence + is_remittance;  │
                 │   then deterministic checks                │
                 └────────────────────────────────────────────┘
                                  │  extraction.status = pending_review
                                  ▼
                 ┌────────────────────────────────────────────┐
                 │ review/   web UI: original ‖ editable      │
                 │   canonical form; flags; approve / edit+   │
                 │   approve / reject; every edit audited     │
                 └────────────────────────────────────────────┘
                                  │  approve → delivery.status = pending
                                  ▼
                 ┌────────────────────────────────────────────┐
                 │ deliver/  POST canonical JSON → backend;   │
                 │   retry w/ backoff; idempotency key =      │
                 │   extraction id; failures → UI            │
                 └────────────────────────────────────────────┘
                                  ▼
                 ┌────────────────────────────────────────────┐
                 │ stub_backend/  separate app, validates the │
                 │   SAME canonical schema → contract doc     │
                 └────────────────────────────────────────────┘
```

`schema/` holds one canonical Pydantic model → JSON Schema, referenced
by: normalization structured output, review-form validation, stub
backend validation, and the contract document.

### Repo layout

```
ar_pipeline/
  ingest/    graph_client.py  poller.py
  classify/  classifier.py
  extract/   base.py  excel.py  html_table.py  pdf.py  vision.py
  normalize/ normalizer.py  llm_client.py  validators.py
  review/    app.py  web/  auth.py
  deliver/   deliverer.py  backend_client.py
  schema/    canonical.py
  db/        base.py  models.py
  worker.py  config.py
migrations/    Alembic (repo root — Alembic convention)
stub_backend/  app.py
tests/
```

> Amendments during implementation (Foundation plan, 2026-09-09):
> Alembic migrations live at repo-root `migrations/`, not `ar_pipeline/db/migrations/`.
> Local dev + tests use the `pgserver` package (embedded PostgreSQL) because the
> build host has no Docker; production still targets a managed Postgres.
> `Settings.database_url` is required (no default) — fail fast on misconfig.
> `graph_client_secret` and `backend_auth_header` are `SecretStr`.

> Amendments during implementation (Classify & Extract plan, 2026-09-09),
> after reviewing real client sample emails:
> - **Canonical schema v2.** One `RemittancePayload` == one payment
>   (`envelope.payment_index` distinguishes payments from the same email —
>   vendors combine several payments, each with its own UTR/date, in one
>   email). Deductions are a **list of typed items** at both header and
>   line-item level: `Deduction{type: tds|credit_note|advance_adjustment|
>   discount|rounding|other, amount, reason}` (TDS u/s 194Q, credit notes,
>   and advance adjustments routinely stack on one invoice line). `Header.
>   payment_reference` and `payment_date` are now **optional** — some
>   advices carry no bank UTR (only "RTGS PAYMENT" or an internal request
>   number); `payment_reference_type` records what the reference is.
> - **`extraction_source.kind` gains `body_text`** — vendor remittances in
>   the email body are often free-text prose, not an HTML table. Migration
>   `0002_add_body_text_kind`.
> - **`RawExtraction`** carries `{text, tables, meta}` — no `images` field.
>   The extractor dataclass is named `ExtractedContent` in code (the ORM
>   row keeps the name `RawExtraction`). The vision extractor consumes the
>   attachment and returns its transcription as `text`; downstream
>   normalization treats vision output like any other raw extraction.
> - **HTML body tables** are parsed with BeautifulSoup + lxml, not
>   `pandas.read_html` (avoids the pandas/numpy dependency).
> - **`pdf_scanned` is NOT rasterised to page images.** The vision
>   extractor sends `application/pdf` as a Claude `document` block and
>   `image/*` as an `image` block (mapping `image/jpg`→`image/jpeg`;
>   media types Claude's vision API can't take raise `VisionUnsupportedMedia`
>   so the classifier skips them). Multi-page scanned advices are handled
>   in one call.
> - **Deduction sign convention:** every `Deduction.amount` is a
>   non-negative `Decimal` — the amount *withheld/subtracted*. The identity
>   the normalizer must satisfy per line item is
>   `invoice_amount - sum(deductions) == amount_paid`, and per payment
>   `total_paid_amount == sum(line_items.amount_paid) - sum(header.deductions)`.
>   A vendor credit note is `Deduction{type: "credit_note"}`, not a
>   negative line.
> - **Per-source failure isolation** (spec's `extract/` rule): one bad
>   attachment fails only its own `extraction_source`; sibling sources of
>   the same email still get their `raw_extraction`. The email goes to
>   `error` (a human retries the failed source via the review UI); already-
>   extracted siblings are not re-run (and re-billed) on retry.
> - `advance_once` commits per email so a slow/blocking vision call does
>   not hold one Postgres transaction open across a whole batch.
> - `advance_once` sends an email with **no non-skipped sources** to
>   `error` (`error_detail = "no extractable content"`), not `extracted`.
> - The AR mailbox mostly receives vendor advices **forwarded by internal
>   staff**, so the envelope sender is usually internal — `vendor_guess`
>   must come from message content, not the `From` address.

## Modules

### ingest/

- `graph_client.py` — MSAL app-only auth (token cache + refresh);
  wraps the Graph calls needed: delta query on
  `/users/{mailbox}/mailFolders/inbox/messages/delta`, fetch message
  detail, download attachments.
- `poller.py` — read saved delta token from `poll_state`; for each new
  message, INSERT an `email` row (`status=new`), download attachments
  to blob storage (record URL + sha256), save the new delta token.
  Dedup via a unique constraint on `internet_message_id` — re-polling
  is a no-op.
- Errors: `429`/throttling → respect `Retry-After`; delta token `410
  Gone` → discard token and do a full resync.
- **Per-message isolation:** a single malformed message (bad/hostile
  attachment name, missing field, transient fetch error) must not abort
  the batch or block the delta token. Wrap each message: log + count as
  `failed`, continue. The delta token advances even if some messages
  failed — a poisoned message re-fetched forever would otherwise stall
  all ingestion (the shared mailbox accepts mail from anyone).
- Attachment blob keys are unique per attachment (keyed by the
  `attachment` row id, not the filename — vendors send duplicate
  filenames). Attachment filenames are reduced to a basename before use.
- Inline attachments (`hasAttachments == false` but `cid:` images in the
  body) are still fetched — vendors paste payment tables as screenshots.

### classify/

- Input: one `email` + its `attachment` rows.
- Output: a list of `extraction_source` rows, each
  `{kind, ref, skipped, skip_reason}` where `kind ∈ {body_table,
  excel, pdf_text, pdf_scanned, image}` and `ref` is `'body'` or an
  attachment id.
- Heuristics: attachment content-type / extension; for PDFs, probe for
  an extractable text layer (pdfplumber) → `pdf_text` vs
  `pdf_scanned`; detect `<table>` in body HTML → `body_table`; skip
  images below a size threshold and known boilerplate
  (logos, signatures, disclaimers) with `skipped=true` + reason.
- Ambiguous → still emit a source, let review catch it.

### extract/

- `base.py` — `RawExtraction` dataclass `{tables: list[list[list[str]]],
  text: str, images: list[bytes]}` and an `Extractor` protocol
  `extract(source, blob) -> RawExtraction`.
- `excel.py` — openpyxl, all sheets → tables.
- `html_table.py` — `pandas.read_html` on the body + surrounding text.
- `pdf.py` — pdfplumber text + tables for `pdf_text`; for
  `pdf_scanned`, hand page images to the vision path.
- `vision.py` — send image(s) to the LLM vision client (swappable),
  return structured content as a `RawExtraction`.
- Output persisted as `raw_extraction.payload` (jsonb) with an
  `extractor_version`.
- A corrupt file fails only its own source; siblings continue.

### normalize/

- `llm_client.py` — `LLMClient` protocol: `parse(*, system, user,
  output_model: type[BaseModel]) -> BaseModel` (structured output).
  `AnthropicLLMClient` (lazy client, `messages.parse`) is the first
  implementation; the interface keeps the provider swappable.
  Raises `LLMRefused` / `LLMTruncated` / `LLMError` (the last also wraps
  API status, timeout, connection, and response-validation failures).
- `normalizer.py` — `build_user_message` renders the raw extractions
  (deterministic source order) + sender + subject. `SYSTEM_PROMPT`
  (versioned, `PROMPT_VERSION`) instructs the model; the LLM returns a
  `NormalizerOutput` (`is_remittance`, `notes`, a list of `PaymentDraft`).
  One payload per distinct payment. `normalize_email` fills the
  `Envelope` we own (uuid `extraction_id`, `source_email_id`, contiguous
  0-based `payment_index`), builds each `RemittancePayload`, runs the
  validators, and returns overall per-payment `confidence` (0-1) — not
  per-field confidence, which an LLM cannot give reliably.
- `validators.py` — deterministic post-checks: the line identity
  `invoice_amount - sum(deductions) == amount_paid` and the payment
  identity `total_paid_amount == sum(line amount_paid) - sum(header
  deductions)`, both within `Decimal("0.02")`; parseable dates and
  payment-date sanity; single currency; negative amounts; duplicate /
  empty invoice numbers. Emits `validation_flags` (`CHECK_VERSION`).
- Writes an `extraction` row: `canonical` (jsonb), `confidence`,
  `is_remittance`, `validation_flags`, `llm_model`, `prompt_version`,
  `status=pending_review`.
- LLM timeout → retry with backoff. Output that fails schema
  validation → still create the `extraction` at `pending_review` with
  the raw LLM response attached; never crash the loop.

### review/

- FastAPI routes + a small web UI (`web/`).
- `auth.py` — `AuthProvider` protocol. Start with a simple provider
  (config-defined users or a single shared login); Entra ID OIDC slots
  in without touching routes.
- List view: pending extractions sorted by received time / confidence,
  showing `validation_flags` and `is_remittance` warnings.
- Detail view: original on the left (rendered email HTML / attachment /
  image), editable canonical form on the right, inline confidence and
  flag annotations.
- Actions: **Approve** (`status=approved`, insert `delivery`
  `pending`), **Edit + Approve**, **Reject** (`status=rejected` +
  reason), **Reprocess** (re-run extract + normalize).
- Every field change writes an `extraction_edit` audit row
  (`field_path`, `old_value`, `new_value`, `edited_by`, `edited_at`).
- Errors tab: emails/extractions in `error`, with detail and a
  **Retry** button. Failed-deliveries list with **Resend**.

### deliver/

- `backend_client.py` — POST canonical JSON to the configured backend
  URL with a configured auth header; sends the `extraction` id as an
  idempotency key.
- `deliverer.py` — process `delivery` rows where `status=pending` and
  `next_attempt_at ≤ now`. `2xx` → `delivered`. `4xx` (not `429`) →
  `failed` (human, surfaced in UI). `5xx`/`429`/timeout → `attempts++`,
  back off on `1m, 5m, 30m, 2h, 6h`, then `failed`.

### stub_backend/

- Separate minimal FastAPI app. `POST /remittances` validates the body
  against the canonical JSON schema, stores it (table or log), returns
  `201`. Used for end-to-end tests now; the schema + this contract is
  handed to the real backend team later.

### schema/

- `canonical.py` — Pydantic models (`Envelope`, `Header`, `LineItem`,
  `RemittancePayload`) that generate the JSON Schema. Single source of
  truth.

## Data model (Postgres)

```
poll_state    delta_token, last_poll_at                     (singleton)

email         id, internet_message_id UNIQUE, sender_address,
              sender_domain, subject, received_at, body_html,
              body_text, raw_headers, status, error_detail, created_at
              status: new → classified → extracted → normalized
                      → review → done | error

attachment    id, email_id, filename, content_type, size,
              blob_url, sha256

extraction_source
              id, email_id, kind, ref, skipped, skip_reason

raw_extraction
              id, extraction_source_id, payload jsonb, extractor_version

extraction    id, email_id, canonical jsonb, confidence numeric,
              is_remittance bool, validation_flags jsonb, llm_model,
              prompt_version, status, reviewed_by, reviewed_at,
              raw_llm_response jsonb, created_at
              status: pending_review → approved | rejected

extraction_edit  (audit)
              id, extraction_id, field_path, old_value, new_value,
              edited_by, edited_at

delivery      id, extraction_id, status, attempts, next_attempt_at,
              last_error, last_attempt_at, delivered_at
              status: pending → delivered | failed

vendor        id, name, sender_domains[], format_hint,
              column_hints jsonb, active
```

- Attachments in blob storage; table holds URL + sha256 (dedup +
  integrity).
- `raw_extraction.payload` and `extraction.canonical` are `jsonb` —
  varied vendor data stays queryable without rigid columns.
- Amounts `NUMERIC`; dates ISO `DATE` strings in JSON; currency
  ISO-4217 (default `INR`).

## Canonical schema (draft)

```json
{
  "envelope": {
    "extraction_id": "string",
    "source_email_id": "string",
    "vendor_guess": "string|null",
    "extracted_at": "ISO-8601",
    "reviewed_by": "string|null"
  },
  "header": {
    "payer_name": "string",
    "payer_id": "string|null",
    "payment_reference": "string",
    "payment_date": "YYYY-MM-DD",
    "payment_method": "string|null",
    "currency": "INR",
    "total_paid_amount": "number"
  },
  "line_items": [
    {
      "invoice_number": "string",
      "invoice_date": "YYYY-MM-DD|null",
      "invoice_amount": "number",
      "discount_taken": "number|null",
      "deduction_amount": "number|null",
      "deduction_reason": "string|null",
      "amount_paid": "number"
    }
  ]
}
```

Field names and additions (PO number, remittance advice number, etc.)
to be finalized against real vendor samples during implementation.

## Processing model

`worker.py` runs three jobs on timers inside the FastAPI process:

- **poll_inbox()** — every ~5 min. Graph delta query; insert new
  emails; download attachments; save delta token.
- **advance_pipeline()** — every ~1 min. Select emails not in
  (`done`, `error`), oldest first, small batch; run the single next
  step for each inside a transaction. A step that raises →
  `status=error`, `error_detail` saved, continue.
- **run_deliveries()** — every ~1 min. Process due `delivery` rows per
  the backoff ladder above.

Properties:

- **Crash-safe:** status advances only inside a committed transaction;
  a restart re-runs the last unfinished step.
- **Idempotent:** `internet_message_id` unique constraint; delivery
  idempotency key = extraction id.
- **Observable:** all state in one database.

Limits (acceptable for Phase 1): single-process, no parallel
extraction; no polling while the app is down (delta query catches up on
restart). Promotion path: move the status-column queue to Redis +
worker processes (brainstorming Approach B) if volume grows.

**Phase 2 hook:** after `normalize`, if `confidence ≥ threshold` and no
`validation_flags` and `is_remittance`, skip `review` and insert a
`pending` delivery directly.

## Error handling

| Area | Handling |
|---|---|
| Graph throttling | respect `Retry-After` |
| Delta token `410` | discard, full resync |
| Graph auth | MSAL token cache + refresh |
| Unknown attachment type | `extraction_source.skipped` + reason; review shows "N attachments couldn't be read" |
| Tiny image / disclaimer | skipped, not an error |
| Corrupt xlsx / pdf | that source errors; siblings continue |
| Scanned PDF, no text layer | routed to LLM vision |
| LLM timeout / connection error | the SDK retries (`max_retries=2`); a persistent failure → `LLMError` → email `error` |
| LLM output truncated (`max_tokens`) | `LLMTruncated` → email `error` (surfaced in the review UI's error tab) |
| LLM per-*payment* output fails the canonical schema | that payment is skipped; a `"schema validation failed"` note is attached to the surviving rows (or, if none survive, to the single `is_remittance=false` row) |
| LLM *whole-response* fails `NormalizerOutput` | wrapped as `LLMError` → email `error` with the validation detail (structured outputs make this rare) |
| `is_remittance = false` / no payments | one `extraction` row at `pending_review`, `canonical={}`, raw response attached, flagged |
| Totals / dates off | `validation_flags`; still reviewable |
| Delivery `5xx`/`429`/timeout | backoff ladder, then `failed` |
| Delivery `4xx` | `failed` immediately; surfaced in UI |
| A stage fails for one email | that email → `error` with the exception detail; **no automatic retry** — a human reprocesses it from the review UI (a per-stage attempt counter is a possible future addition). Other emails in the batch are unaffected (per-email savepoint + commit). |

Nothing is dropped silently — every failure lands in a queryable state
with a UI affordance to retry.

## Testing

TDD throughout.

| Target | Approach |
|---|---|
| Extractors | real fixture files (xlsx, text pdf, scanned pdf, html-body email, image) → assert `RawExtraction` shape |
| Classifier | table: email fixture → expected list of sources |
| Normalize | golden tests (fixture raw → expected canonical, LLM mocked) + a small live eval set run on demand and scored |
| Validators | totals mismatch, unparseable dates, currency drift |
| Deliver | mock backend: retry, backoff, idempotency header, 4xx vs 5xx |
| Graph client | recorded HTTP fixtures / fake |
| Integration | seed a fake email → whole pipeline → assert a `delivered` row in the stub backend |
| Review UI | route tests: list, edit + audit, approve → delivery, reject, retry |

### Test data

A handful of sample emails, one per format, committed as fixtures —
synthetic to start, redacted real vendor emails swapped in as they
arrive. These double as the normalization eval set. **The user will be
asked to upload real samples when implementation reaches the `extract/`
and `normalize/` modules.**

## Open items for implementation

- Finalize canonical field names against real samples.
- Confirm cloud provider (leaning Azure) and blob store choice.
- Confirm the app auth provider for Phase 1 (simple vs Entra ID now).
- Backend URL, auth scheme, and idempotency-key header name (stub
  first; real values later).
- LLM model selection and the normalization prompt (tuned against the
  eval set).
- Azure app registration for Graph (`Mail.Read`, application access
  policy scoping).
