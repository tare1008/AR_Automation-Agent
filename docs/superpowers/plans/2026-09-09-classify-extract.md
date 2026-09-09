# Classify & Extract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For each stored email, decide which "extraction sources" it contains (Excel / PDF / body table / body free-text / image) and mechanically extract the raw content of each into `raw_extraction` rows — so the normalization step (next plan) has structured raw material to work from. Also evolve the canonical schema to match what real vendor emails actually contain.

**Architecture:** A `classify_email()` pure function turns one `email` + its `attachment` rows into a list of `extraction_source` rows. One extractor per source *kind*, each a pure function `bytes|str -> ExtractedContent`, selected by kind. Deterministic extractors (openpyxl, BeautifulSoup, pdfplumber) for structured formats; an LLM-vision extractor for images and scanned PDFs. `worker.advance_pipeline()` walks emails `new → classified → extracted`, one step per tick, per-email error isolation.

**Tech Stack:** Python 3.12, openpyxl, pdfplumber, beautifulsoup4 + lxml, `anthropic` SDK (vision), SQLAlchemy 2.0 (sync), pytest. Dev: reportlab (fixture regen).

**Spec:** `docs/superpowers/specs/2026-09-09-ar-email-extraction-design.md` (`classify/`, `extract/`, and the amendment block after the Repo layout diagram)

**Builds on:** Foundation + Ingestion (both merged to `master`). Available:
- `ar_pipeline.db.models`: `Email` (`body_html`, `body_text`, `status` default `"new"`, statuses `new→classified→extracted→normalized→review→done|error`), `Attachment` (`email_id`, `filename`, `content_type`, `size`, `blob_url`, `sha256`), `ExtractionSource` (`email_id`, `kind` CHECK-constrained to `body_table|excel|pdf_text|pdf_scanned|image`, `ref`, `skipped`, `skip_reason`), `RawExtraction` (`extraction_source_id`, `payload` jsonb (MutableDict), `extractor_version`).
- `ar_pipeline.db.base`: `get_session()`, `get_engine()`, `reset_engine()`.
- `ar_pipeline.storage`: `BlobStore` (`put`, `get`, `sha256`), `LocalBlobStore`, `get_blob_store()`.
- `ar_pipeline.ingest`: `poll_once(graph, blob_store, session)`, `tests/ingest/fakes.FakeGraphClient` (`add_message`, `redeliver_all`), `ar_pipeline.ingest.types.{GraphMessage, GraphAttachment}`.
- `ar_pipeline.config.get_settings()` → `Settings` (`llm_provider="anthropic"`, `blob_dir`, ...).
- `ar_pipeline.schema.canonical`: `RemittancePayload`, `Envelope`, `Header`, `LineItem`, `CANONICAL_JSON_SCHEMA`.
- `ar_pipeline.worker`: `poll_inbox()` (wired), `advance_pipeline()` / `run_deliveries()` (still no-ops), `build_scheduler()`.
- Dev tooling: `ruff`, `mypy` (checks `ar_pipeline`, `stub_backend`, **and `tests`**), `.github/workflows/ci.yml`.
- Test fixtures: `tests/fixtures/emails/*.eml` (6 synthetic .eml files — see `tests/fixtures/emails/README.md` for the format-coverage table), `tests/fixtures/_generator/build_fixtures.py`.
- Test infra: `tests/conftest.py` → session-scoped autouse `_embedded_pg`, `_test_engine`, function-scoped `db_session` (savepoint rollback).

## Global Constraints

- Python 3.12; deps via `uv`.
- Sync SQLAlchemy only. No import-time DB connections or network calls in `ar_pipeline/`.
- No real network in tests: the Anthropic vision client is tested with `unittest.mock` patching `anthropic.Anthropic`; a live test is gated behind `ANTHROPIC_API_KEY` + `@pytest.mark.live` and skipped by default.
- Every task ends with `uv run ruff check`, `uv run ruff format --check`, `uv run mypy` (bare — checks tests too), `uv run pytest` — all clean.
- Commit message trailer: `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` and nothing else. Verify with `git log -1 --format=%B` after each commit; `git commit --amend` if wrong.
- Monetary values: `Decimal`, never `float`. Indian digit grouping (`1,36,70,691.00`) and Western grouping both occur — parse by stripping non-digit/non-`.`/non-`-` characters.
- New code: `ar_pipeline/classify/`, `ar_pipeline/extract/`, `ar_pipeline/pipeline/`. One responsibility per file.

---

### Task 1: Canonical schema v2 (deductions, multi-payment, nullable UTR)

Real vendor emails (see `tests/fixtures/emails/`) revealed: one email can hold **multiple payments** (different UTRs/dates); deductions are **layered** (TDS + credit note + advance adjustment on one line); the **UTR is sometimes absent**. The schema must reflect this before normalization is built.

**Files:**
- Modify: `ar_pipeline/schema/canonical.py`
- Modify: `ar_pipeline/schema/__init__.py`
- Modify: `tests/schema/test_canonical.py`
- Modify: `tests/stub_backend/test_app.py` (payloads use the new shape)
- Modify: `stub_backend/app.py` only if its `_payload` helper needs it (it validates `RemittancePayload`, so only the test payloads change)

**Interfaces:**
- Produces:
  - `Deduction(BaseModel)` — `type: DeductionType`, `amount: Decimal`, `reason: str | None = None`. `DeductionType = Literal["tds", "credit_note", "advance_adjustment", "discount", "rounding", "other"]`.
  - `LineItem` — `invoice_number: str`, `invoice_date: date | None = None`, `invoice_amount: Decimal`, `deductions: list[Deduction] = []`, `amount_paid: Decimal`. (Replaces the old `discount_taken`/`deduction_amount`/`deduction_reason` fields.)
  - `Header` — `payer_name: str`, `payer_id: str | None = None`, `payment_reference: str | None = None`, `payment_reference_type: str | None = None`, `payment_date: date | None = None`, `payment_method: str | None = None`, `currency: _Currency = "INR"`, `total_paid_amount: Decimal`, `deductions: list[Deduction] = []`.
  - `Envelope` — adds `payment_index: int = 0` (0-based; which payment within the source email). Keeps `extraction_id`, `source_email_id`, `vendor_guess`, `extracted_at`, `reviewed_by`.
  - `RemittancePayload` — unchanged shape (`envelope`, `header`, `line_items: list[LineItem] = Field(min_length=1)`), all models keep `ConfigDict(extra="forbid")`.
  - `CANONICAL_JSON_SCHEMA = RemittancePayload.model_json_schema()`.
- **No DB migration** — `extraction` already allows N rows per `email`; one `extraction` row = one `RemittancePayload` = one payment.

- [ ] **Step 1: Update the failing tests**

`tests/schema/test_canonical.py` — rewrite `_valid_payload_dict()` for the new shape and adjust/extend tests:
```python
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ar_pipeline.schema.canonical import (
    CANONICAL_JSON_SCHEMA,
    Deduction,
    Header,
    LineItem,
    RemittancePayload,
)


def _valid_payload_dict():
    return {
        "envelope": {
            "extraction_id": "ext-1",
            "source_email_id": "email-1",
            "payment_index": 0,
            "vendor_guess": "Fluorochem Industries",
            "extracted_at": datetime(2026, 2, 18, tzinfo=timezone.utc).isoformat(),
            "reviewed_by": None,
        },
        "header": {
            "payer_name": "Fluorochem Industries",
            "payment_reference": "STBK52026021813360279",
            "payment_reference_type": "utr",
            "payment_date": "2026-02-18",
            "payment_method": "RTGS",
            "currency": "INR",
            "total_paid_amount": "13670691.00",
            "deductions": [{"type": "tds", "amount": "17832.00", "reason": "IT TDS 194Q 0.1%"}],
        },
        "line_items": [
            {
                "invoice_number": "FCI2510007033",
                "invoice_date": "2026-02-02",
                "invoice_amount": "1452299.16",
                "deductions": [],
                "amount_paid": "1452299.16",
            }
        ],
    }


def test_valid_payload_parses():
    p = RemittancePayload.model_validate(_valid_payload_dict())
    assert p.header.total_paid_amount == Decimal("13670691.00")
    assert p.header.deductions[0].type == "tds"
    assert p.envelope.payment_index == 0


def test_payment_reference_is_optional():
    d = _valid_payload_dict()
    d["header"]["payment_reference"] = None
    d["header"]["payment_date"] = None
    RemittancePayload.model_validate(d)  # sample 03 / 06 have no real UTR


def test_line_item_deductions_default_empty_and_typed():
    li = LineItem.model_validate(
        {"invoice_number": "X", "invoice_amount": "100.00", "amount_paid": "90.00",
         "deductions": [{"type": "credit_note", "amount": "10.00"}]}
    )
    assert li.deductions[0].amount == Decimal("10.00")
    assert li.deductions[0].reason is None


def test_bad_deduction_type_rejected():
    with pytest.raises(ValidationError):
        Deduction.model_validate({"type": "vat", "amount": "1.00"})


def test_currency_defaults_to_inr_and_uppercases():
    h = Header(payer_name="X", payment_reference=None, payment_date=None,
               total_paid_amount=Decimal("1.00"))
    assert h.currency == "INR"
    d = _valid_payload_dict()
    d["header"]["currency"] = "inr"
    assert RemittancePayload.model_validate(d).header.currency == "INR"


def test_extra_field_forbidden():
    d = _valid_payload_dict()
    d["header"]["mystery"] = 1
    with pytest.raises(ValidationError):
        RemittancePayload.model_validate(d)


def test_line_items_min_length_one():
    d = _valid_payload_dict()
    d["line_items"] = []
    with pytest.raises(ValidationError):
        RemittancePayload.model_validate(d)


def test_json_schema_shape():
    assert CANONICAL_JSON_SCHEMA["title"] == "RemittancePayload"
    assert "Deduction" in CANONICAL_JSON_SCHEMA["$defs"]
```

- [ ] **Step 2: Run — RED**

`uv run pytest tests/schema/test_canonical.py -v` → fails (old fields, no `Deduction`).

- [ ] **Step 3: Rewrite `ar_pipeline/schema/canonical.py`**

```python
"""Canonical remittance payload — one payload == one payment. The single
source of truth for extracted settlement data; referenced by normalization,
the review UI, and the stub backend."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

_Currency = Annotated[str, StringConstraints(pattern=r"^[A-Za-z]{3}$", to_upper=True)]

DeductionType = Literal[
    "tds", "credit_note", "advance_adjustment", "discount", "rounding", "other"
]


class Deduction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: DeductionType
    amount: Decimal
    reason: str | None = None


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extraction_id: str
    source_email_id: str
    payment_index: int = 0
    vendor_guess: str | None = None
    extracted_at: datetime
    reviewed_by: str | None = None


class Header(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payer_name: str
    payer_id: str | None = None
    payment_reference: str | None = None
    payment_reference_type: str | None = None
    payment_date: date | None = None
    payment_method: str | None = None
    currency: _Currency = "INR"
    total_paid_amount: Decimal
    deductions: list[Deduction] = Field(default_factory=list)


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_number: str
    invoice_date: date | None = None
    invoice_amount: Decimal
    deductions: list[Deduction] = Field(default_factory=list)
    amount_paid: Decimal


class RemittancePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope: Envelope
    header: Header
    line_items: list[LineItem] = Field(min_length=1)


CANONICAL_JSON_SCHEMA: dict = RemittancePayload.model_json_schema()
```

Update `ar_pipeline/schema/__init__.py` to also export `Deduction` and `DeductionType` (add to imports and `__all__`).

- [ ] **Step 4: Fix `tests/stub_backend/test_app.py`**

Its `_payload()` helper builds a `RemittancePayload` dict — update it to the new shape (mirror `_valid_payload_dict` above, minimal: one line item, `deductions: []`, `payment_index: 0`). Do not weaken any assertion; the tests still check `201` / `422` / idempotency / `GET` 404.

- [ ] **Step 5: Run — GREEN + gates**

```
uv run pytest tests/schema/test_canonical.py tests/stub_backend/ -v
uv run pytest -q
uv run ruff check && uv run ruff format --check && uv run mypy
```
All clean.

- [ ] **Step 6: Commit**

```bash
git add ar_pipeline/schema tests/schema tests/stub_backend
git commit -m "feat: canonical schema v2 — layered deductions, multi-payment, optional UTR"
```

---

### Task 2: Fixture loader — feed .eml files through the real poller

**Files:**
- Create: `tests/fixtures/__init__.py`
- Create: `tests/fixtures/loader.py`
- Create: `tests/fixtures/test_loader.py`

**Interfaces:**
- Produces:
  - `tests/fixtures/loader.eml_to_graph(path) -> tuple[GraphMessage, list[GraphAttachment]]` — parse a `.eml` with `email.message_from_bytes(..., policy=email.policy.default)`; map to the ingest dataclasses. `GraphMessage.id` = a stable slug from the filename; `internet_message_id` = the file's `Message-ID` header (or a slug); `sender_address` from `From`; `received_at` from `Date` (`email.utils.parsedate_to_datetime`); `body_html` / `body_text` from `msg.get_body(preferencelist=...)`; `has_attachments` = any real attachment part. `GraphAttachment` list from `msg.iter_attachments()` where `get_filename()` is set and `get_content_disposition() == "attachment"` — decode `get_payload(decode=True)`. **Inline images** (`Content-ID` present / disposition `inline`) are also returned as `GraphAttachment` (the poller stores everything; the classifier decides what to skip).
  - `tests/fixtures/loader.load_email(name, session, blob_store) -> Email` — call `eml_to_graph`, wrap in `FakeGraphClient([msg], {msg.id: atts})`, run `poll_once(fake, blob_store, session)`, `session.flush()`, return the created `Email` (query by `internet_message_id`).
  - `tests/fixtures/loader.FIXTURE_NAMES: list[str]` — the 6 basenames without extension, sorted.

- [ ] **Step 1: Write the failing test**

`tests/fixtures/test_loader.py`:
```python
import pytest
from sqlalchemy import select

from ar_pipeline.db.models import Attachment, Email
from ar_pipeline.storage import LocalBlobStore
from tests.fixtures.loader import FIXTURE_NAMES, eml_to_graph, load_email


def test_fixture_names():
    assert FIXTURE_NAMES == [
        "01_nordicauto_hsbc_pdf", "02_fluorochem_body_table", "03_contibus_pdf",
        "04_sunrise_body_multi_payment", "05_bharat_body_freetext", "06_zenith_excel",
    ]


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_every_fixture_loads_into_the_db(name, db_session, tmp_path):
    email = load_email(name, db_session, LocalBlobStore(str(tmp_path)))
    db_session.flush()
    assert email.status == "new"
    assert email.sender_address
    assert email.body_html or email.body_text


def test_pdf_fixture_has_attachment_stored(db_session, tmp_path):
    store = LocalBlobStore(str(tmp_path))
    email = load_email("01_nordicauto_hsbc_pdf", db_session, store)
    db_session.flush()
    atts = db_session.scalars(
        select(Attachment).where(Attachment.email_id == email.id)
    ).all()
    names = {a.filename for a in atts}
    assert "Payment_Advice.pdf" in names
    pdf = next(a for a in atts if a.filename == "Payment_Advice.pdf")
    assert store.get(pdf.blob_url.split("//", 1)[1] if "://" not in pdf.blob_url else pdf.blob_url)  # smoke: blob exists


def test_body_only_fixture_has_no_attachments(db_session, tmp_path):
    email = load_email("05_bharat_body_freetext", db_session, LocalBlobStore(str(tmp_path)))
    db_session.flush()
    assert db_session.scalars(select(Attachment).where(Attachment.email_id == email.id)).all() == []


def test_eml_to_graph_maps_sender_and_body():
    msg, atts = eml_to_graph_path("02_fluorochem_body_table")
    assert "acmemetals.example" in msg.sender_address
    assert "remitted" in msg.body_html.lower()


def eml_to_graph_path(name):
    from pathlib import Path
    return eml_to_graph(Path(__file__).parent / "emails" / f"{name}.eml")
```

- [ ] **Step 2: RED** — `uv run pytest tests/fixtures/test_loader.py -v` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement `tests/fixtures/loader.py`**

Use stdlib `email` (`policy=email.policy.default`). Notes:
- `_slug(name)` for `GraphMessage.id` — strip extension, keep `[a-z0-9_]`.
- `internet_message_id`: `msg["Message-ID"]` or `f"<{slug}@fixture>"`.
- `received_at`: `email.utils.parsedate_to_datetime(msg["Date"])`; if naive, attach `UTC`.
- body: `msg.get_body(preferencelist=("html",))` → `.get_content()` for `body_html`; `("plain",)` for `body_text`; empty string when absent.
- `has_attachments`: `any(True for _ in msg.iter_attachments())` OR any `image/*` part with a `Content-ID`.
- attachments: iterate `msg.walk()`; for parts where `get_content_maintype() != "multipart"` and (`get_content_disposition() == "attachment"` OR (`get_content_maintype() == "image"` and `part["Content-ID"]`)), build `GraphAttachment(name=get_filename() or "inline", content_type=get_content_type(), size=len(data), content=data)`.
- `FIXTURE_NAMES` = `sorted(p.stem for p in (Path(__file__).parent/"emails").glob("*.eml"))`.

- [ ] **Step 4: GREEN + gates**, then commit:
```bash
git add tests/fixtures
git commit -m "test: fixture loader — .eml through the real poller into the DB"
```

---

### Task 3: The classifier

**Files:**
- Create: `ar_pipeline/classify/__init__.py`
- Create: `ar_pipeline/classify/classifier.py`
- Create: `tests/classify/__init__.py`
- Create: `tests/classify/test_classifier.py`

**Interfaces:**
- Consumes: `Email`, `Attachment`, `BlobStore`.
- Produces:
  - `ar_pipeline.classify.classifier.SourceSpec` — frozen dataclass: `kind: str` (one of `body_table | body_text | excel | pdf_text | pdf_scanned | image`), `ref: str` (`"body"` or an attachment id as `str`), `skipped: bool = False`, `skip_reason: str | None = None`.
  - `ar_pipeline.classify.classifier.classify_email(email: Email, attachments: list[Attachment], blob_store: BlobStore) -> list[SourceSpec]`.
  - `ar_pipeline.classify.classifier.pdf_has_text_layer(data: bytes) -> bool` — `pdfplumber.open`; `True` if concatenated `page.extract_text()` across pages has ≥ 20 non-whitespace chars.

> Note: the `ExtractionSource` model CHECK constraint currently allows only
> `body_table | excel | pdf_text | pdf_scanned | image`. This task also
> **adds `body_text`** to that CHECK — a one-line model change plus an
> Alembic migration `0002_add_body_text_kind` (`op.drop_constraint` +
> `op.create_check_constraint` with the new tuple; downgrade reverses).
> Regenerate is not needed — hand-write the migration; verify `uv run
> alembic check` is clean afterward against the throwaway migrations DB
> (see Ingestion's `tests/db/test_migrations.py` for the pattern).

**Classification rules:**
- **Each attachment:**
  - extension/content-type `xlsx`/`xls`/`spreadsheetml` → `excel`.
  - `pdf` (by content-type `application/pdf` OR `application/octet-stream` with a `.pdf` filename) → fetch bytes, `pdf_has_text_layer` → `pdf_text` else `pdf_scanned`.
  - `image/*` → `image`, BUT `skipped=True, skip_reason="inline image below threshold"` when `size < 25_000` AND (has a `Content-ID` / filename matches `r"(?i)(image\d+|logo|signature|outlook-)"`). (The fixture logos are ~70 bytes — skipped.)
  - anything else → emit `image`? no — emit with `kind="pdf_text"` only for pdf; otherwise `skipped=True, skip_reason=f"unsupported attachment type {content_type}"`.
- **Body:** parse `email.body_html` with BeautifulSoup (`lxml`). If it contains a `<table>` whose rows include ≥ 2 cells that look numeric (`r"[\d,]+\.\d{2}"`), emit `SourceSpec("body_table", "body")`. Else, if `email.body_text` (or html-stripped text) has ≥ 120 non-whitespace chars *after* removing quoted-reply lines (lines starting `>` or blocks after a `From:`/`Sent:` header pattern is NOT reliable — just use length + presence of digits) → emit `SourceSpec("body_text", "body")`. Else no body source.
- Always emit at least one non-skipped source if any content exists; if truly nothing, emit `SourceSpec("body_text", "body", skipped=True, skip_reason="no extractable content")`.

- [ ] **Step 1: Write the failing test** — `tests/classify/test_classifier.py`, driven by the fixtures via the Task 2 loader:
```python
import pytest
from sqlalchemy import select

from ar_pipeline.classify.classifier import classify_email
from ar_pipeline.db.models import Attachment
from ar_pipeline.storage import LocalBlobStore
from tests.fixtures.loader import load_email

# name -> the set of (kind, skipped) tuples classify_email must produce
EXPECTED = {
    "01_nordicauto_hsbc_pdf": {("pdf_text", False), ("image", True)},
    "02_fluorochem_body_table": {("body_table", False), ("image", True)},
    "03_contibus_pdf": {("pdf_text", False)},
    "04_sunrise_body_multi_payment": {("body_table", False)},
    "05_bharat_body_freetext": {("body_text", False)},
    "06_zenith_excel": {("excel", False)},
}


@pytest.mark.parametrize("name,expected", EXPECTED.items())
def test_classifier_on_fixtures(name, expected, db_session, tmp_path):
    store = LocalBlobStore(str(tmp_path))
    email = load_email(name, db_session, store)
    db_session.flush()
    atts = db_session.scalars(
        select(Attachment).where(Attachment.email_id == email.id)
    ).all()
    specs = classify_email(email, list(atts), store)
    got = {(s.kind, s.skipped) for s in specs}
    assert got == expected


def test_body_only_email_never_emits_a_body_and_attachment_dupe(db_session, tmp_path):
    store = LocalBlobStore(str(tmp_path))
    email = load_email("06_zenith_excel", db_session, store)
    db_session.flush()
    atts = list(db_session.scalars(select(Attachment)))
    specs = classify_email(email, atts, store)
    # zenith body is one line of boilerplate — no body source
    assert not any(s.kind in ("body_table", "body_text") and not s.skipped for s in specs)
```

(Also add a focused unit test for `pdf_has_text_layer` with a fixture PDF's bytes → `True`, and a 1-page image-only PDF built inline via reportlab-drawn-image or a hand-crafted no-text PDF → `False`.)

- [ ] **Step 2: RED.**

- [ ] **Step 3: model + migration + `classifier.py`.** Add `body_text` to `ExtractionSource` kinds; write `migrations/versions/0002_add_body_text_kind.py`; verify `alembic check`. Implement `classify_email` + helpers per the rules above.

- [ ] **Step 4: GREEN + gates**, commit:
```bash
git add ar_pipeline/classify ar_pipeline/db/models.py migrations tests/classify pyproject.toml uv.lock
git commit -m "feat: email classifier (body/excel/pdf/image source detection)"
```

---

### Task 4: Deterministic extractors

**Files:**
- Create: `ar_pipeline/extract/__init__.py`
- Create: `ar_pipeline/extract/base.py`
- Create: `ar_pipeline/extract/excel.py`
- Create: `ar_pipeline/extract/html_table.py`
- Create: `ar_pipeline/extract/body_text.py`
- Create: `ar_pipeline/extract/pdf.py`
- Create: `tests/extract/__init__.py`
- Create: `tests/extract/test_excel.py`, `test_html_table.py`, `test_body_text.py`, `test_pdf.py`

**Interfaces:**
- Produces:
  - `ar_pipeline.extract.base.ExtractedContent` — frozen dataclass: `text: str`, `tables: list[list[list[str]]]` (each table a list of row-lists of cell strings), `meta: dict[str, object]` (e.g. `{"sheet_names": [...]}`, `{"page_count": 2}`). Method `to_payload(self) -> dict` for the `raw_extraction.payload` jsonb.
  - `ar_pipeline.extract.base.EXTRACTOR_VERSION = "1"` (bump on behavior change; stored in `raw_extraction.extractor_version`).
  - `ar_pipeline.extract.excel.extract_excel(data: bytes) -> ExtractedContent` — openpyxl `load_workbook(BytesIO(data), data_only=True, read_only=True)`; every sheet → a table (rows with any non-`None` cell; each cell `str(c) if c is not None else ""`); `text` = a readable dump of all sheets; `meta["sheet_names"]`.
  - `ar_pipeline.extract.html_table.extract_html_tables(body_html: str) -> ExtractedContent` — BeautifulSoup(`lxml`); every `<table>` → a table (`<tr>` → row, `<td>|<th>` → cell text, whitespace-collapsed); `text` = the page's visible text with `<script>/<style>` removed and whitespace collapsed; `meta["table_count"]`.
  - `ar_pipeline.extract.body_text.extract_body_text(body_text: str, body_html: str) -> ExtractedContent` — prefer `body_text`; if empty, strip tags from `body_html`. Collapse runs of blank lines; keep line structure (the free-text fixture relies on line breaks). `tables=[]`.
  - `ar_pipeline.extract.pdf.extract_pdf(data: bytes) -> ExtractedContent` — pdfplumber; per page `extract_text()` joined into `text` (page-separated by `"\n\n"`), `extract_tables()` appended to `tables`; `meta["page_count"]`.
- None of these touch the DB or network. Each is a pure `bytes|str -> ExtractedContent`.

- [ ] **Step 1: Write failing tests** — one per extractor, asserting against the *known* fixture content. Examples:
  - `test_excel`: load `06_zenith_excel`'s xlsx bytes (via the loader / `iter_attachments`), `extract_excel` → a table containing a row with `"ZCC2610000038"` and one with `"TOTAL"`; `meta["sheet_names"] == ["Sheet1"]`.
  - `test_html_table`: `02_fluorochem_body_table`'s `body_html` → `extract_html_tables` → a table with ≥ 14 rows, one row containing `"FCI2510007033"` and `"1,452,299.16"`; `text` contains `"remitted"` and `"STBK52026021813360279"`.
  - `test_body_text`: `05_bharat_body_freetext` → `extract_body_text` → `text` contains `"PAYMENT DONE Rs. 2743303.70"` and `"UTR): STBK52026032800800086"`; multi-line preserved (`text.count("\n") > 8`).
  - `test_pdf`: `01_nordicauto_hsbc_pdf`'s PDF bytes → `extract_pdf` → `text` contains `"Remittance amount: INR 6,633,624.61"` and `"ACM2510006275"`; `meta["page_count"] == 1`. And `03_contibus_pdf` → `page_count == 2`, `text` contains `"Total"` and a `"DISCO"` line.

- [ ] **Step 2: RED.**  **Step 3:** add deps (`uv add pdfplumber beautifulsoup4 lxml`), implement. **Step 4:** GREEN + gates.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/extract tests/extract pyproject.toml uv.lock
git commit -m "feat: deterministic extractors (excel, html table, body text, pdf)"
```

---

### Task 5: Vision extractor (image / scanned PDF → ExtractedContent via Claude)

**Files:**
- Create: `ar_pipeline/extract/vision.py`
- Create: `tests/extract/test_vision.py`

**Interfaces:**
- Consumes: `ExtractedContent`, `EXTRACTOR_VERSION`, `ar_pipeline.config.get_settings`.
- Produces:
  - `ar_pipeline.extract.vision.VisionExtractor` — `Protocol`: `extract_image(self, data: bytes, media_type: str) -> ExtractedContent`.
  - `ar_pipeline.extract.vision.AnthropicVisionExtractor` — real impl. `__init__(self, client: "anthropic.Anthropic | None" = None, model: str = "claude-opus-5")`. `extract_image`: base64-encode; one `client.messages.create` call, `model=self._model`, `max_tokens=8000`, a `system` instructing "You are transcribing a payment remittance / settlement document. Output every line of text verbatim, and render any tabular data as pipe-delimited rows (one row per line). Do not summarise, interpret, or omit anything. No commentary." and a user message with an `image` block + `"Transcribe this document."`. Parse: `text` = the response's concatenated `text` blocks; `tables` = `[]` (the normalizer parses the pipe rows out of `text`); `meta = {"model": self._model, "via": "vision"}`. Guard `response.stop_reason` — if `"refusal"`, raise `VisionRefused`.
  - `ar_pipeline.extract.vision.VisionRefused(Exception)`.
  - `ar_pipeline.extract.vision.get_vision_extractor() -> VisionExtractor` — returns `AnthropicVisionExtractor()` (reads `get_settings()` for a future `llm_model` override; for now the default).
  - `tests/extract/vision_fake.FakeVisionExtractor` — returns a canned `ExtractedContent` for tests of Task 6.

Read the bundled `claude-api` skill's `python/claude-api/README.md` (Vision section) — use `anthropic.Anthropic()` + `client.messages.create` with a base64 `image` block. Add `anthropic` to deps (`uv add anthropic`). Default model `claude-opus-5`.

- [ ] **Step 1: Write the failing test** (SDK fully mocked — no network):
```python
from unittest.mock import MagicMock

import pytest

from ar_pipeline.extract.vision import AnthropicVisionExtractor, VisionRefused


def _resp(text, stop="end_turn"):
    r = MagicMock()
    r.stop_reason = stop
    blk = MagicMock()
    blk.type = "text"
    blk.text = text
    r.content = [blk]
    return r


def test_vision_transcribes_text_and_tables():
    client = MagicMock()
    client.messages.create.return_value = _resp(
        "Payment advice\nInvoice | Amount\nINV-1 | 100.00\nINV-2 | 200.00"
    )
    ext = AnthropicVisionExtractor(client=client, model="claude-opus-5")
    raw = ext.extract_image(b"\x89PNG...", "image/png")
    assert "INV-1 | 100.00" in raw.text
    assert raw.meta["via"] == "vision"
    args, kwargs = client.messages.create.call_args
    assert kwargs["model"] == "claude-opus-5"
    content = kwargs["messages"][0]["content"]
    assert any(b.get("type") == "image" for b in content)


def test_vision_raises_on_refusal():
    client = MagicMock()
    client.messages.create.return_value = _resp("", stop="refusal")
    with pytest.raises(VisionRefused):
        AnthropicVisionExtractor(client=client).extract_image(b"x", "image/png")


@pytest.mark.live
def test_vision_live_smoke():
    pytest.importorskip("anthropic")
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("no ANTHROPIC_API_KEY")
    # a tiny generated PNG of the text "INV-42  9,433.00" — assert the number comes back
    ...
```

Register the `live` marker in `pyproject.toml` (`[tool.pytest.ini_options] markers = ["live: hits a real external API"]`) and add `-m "not live"` to `addopts`.

- [ ] **Step 2: RED. Step 3: implement. Step 4: GREEN + gates.**

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/extract/vision.py tests/extract/test_vision.py tests/extract/vision_fake.py pyproject.toml uv.lock
git commit -m "feat: Claude vision extractor for images and scanned PDFs"
```

> **Coverage gap (record, don't fix):** there is no real scanned-PDF /
> photographed-settlement fixture. The vision path is unit-tested with a
> mocked SDK and a gated live smoke test only. Add a real fixture to
> `tests/fixtures/emails/` when one is available and extend
> `tests/classify` + an end-to-end test then.

---

### Task 6: Wire `advance_pipeline()` — classify then extract

**Files:**
- Create: `ar_pipeline/pipeline/__init__.py`
- Create: `ar_pipeline/pipeline/advance.py`
- Modify: `ar_pipeline/worker.py`
- Modify: `tests/test_worker.py`
- Create: `tests/pipeline/__init__.py`
- Create: `tests/pipeline/test_advance.py`
- Create: `tests/pipeline/test_end_to_end.py`

**Interfaces:**
- Produces:
  - `ar_pipeline.pipeline.advance.AdvanceStats` — frozen dataclass: `classified: int`, `extracted: int`, `errored: int`.
  - `ar_pipeline.pipeline.advance.advance_once(session, blob_store, vision_extractor, *, batch: int = 20) -> AdvanceStats` — select emails with `status in ("new", "classified")` (oldest `received_at` first, limit `batch`); for each, inside `session.begin_nested()` + `try/except Exception`:
    - `status == "new"`: `classify_email(...)` → insert `ExtractionSource` rows (map `SourceSpec` → row; `ref` `"body"` stays `"body"`, an attachment id becomes `str(att.id)`) → `email.status = "classified"` → `classified += 1`.
    - `status == "classified"`: for each non-skipped `ExtractionSource`, load its bytes (`ref=="body"` → use `email.body_html/body_text`; else fetch the `Attachment` blob), dispatch by `kind` to the extractor, insert a `ExtractedContent(extraction_source_id=..., payload=raw.to_payload(), extractor_version=...)` → when all non-skipped sources have a `raw_extraction`, `email.status = "extracted"` → `extracted += 1`.
    - on exception: savepoint rolls back, `email.status = "error"`, `email.error_detail = f"{type(exc).__name__}: {exc}"` (this write is OUTSIDE the rolled-back savepoint — re-assign on the live `email` object after the `except`), `errored += 1`.
  - kind → extractor dispatch: `excel`→`extract_excel`, `body_table`→`extract_html_tables(email.body_html)`, `body_text`→`extract_body_text(...)`, `pdf_text`→`extract_pdf`, `pdf_scanned`/`image`→`vision_extractor.extract_image(data, media_type)`.
  - `ar_pipeline.worker.advance_pipeline()` — build `get_blob_store()` + `get_vision_extractor()`, `with get_session() as s: advance_once(s, ...)`, log the stats; still returns `None`. Lazy imports to keep `worker` light.

- [ ] **Step 1: Write failing tests.**
  - `tests/pipeline/test_advance.py`: unit-level — seed one `Email(status="new")` with a `body_text` source via the loader, `advance_once` once → status `classified`, N `ExtractionSource` rows; `advance_once` again → status `extracted`, `raw_extraction` rows exist with non-empty `payload["text"]`. A poison case: monkeypatch one extractor to raise → that email → `status="error"`, `error_detail` set, **other emails in the batch still advance**, `errored == 1`.
  - `tests/pipeline/test_end_to_end.py`: for each of the 6 fixtures — `load_email` → `advance_once` twice (with `FakeVisionExtractor` injected) → assert final `email.status == "extracted"`, and the `raw_extraction` payload for the primary source contains a known invoice number from that fixture (e.g. `06` → `"ZCC2610000038"` somewhere in `payload["text"]` or a table cell; `02` → `"FCI2510007033"`; `05` → `"STBK52026032800800086"`).
  - `tests/test_worker.py`: update — `advance_pipeline()` now calls `ar_pipeline.pipeline.advance.advance_once` (patch it); assert it returns `None` and calls through. `run_deliveries` stays a no-op.

- [ ] **Step 2: RED. Step 3: implement. Step 4: GREEN + gates** (`uv run pytest -q` — expect the count to jump; ruff/mypy clean).

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/pipeline ar_pipeline/worker.py tests/pipeline tests/test_worker.py
git commit -m "feat: advance_pipeline runs classify then extract with per-email isolation"
```

---

## Self-Review

**1. Spec coverage:**
- `classify/` — email + attachments → `extraction_source` rows `{kind, ref, skipped, skip_reason}`; heuristics (content-type, PDF text-layer probe, `<table>` detection, skip small inline images) → Task 3. Spec kinds extended with `body_text` (migration `0002`) — a spec deviation, recorded in the spec amendment block and here. ✓
- `extract/` — one extractor per kind, common `ExtractedContent {tables, text, meta}`; openpyxl / read_html-equivalent (BeautifulSoup) / pdfplumber deterministic; LLM vision for image + scanned PDF → Tasks 4, 5. `ExtractedContent` drops the spec's `images` field (vision consumes images directly and returns text) — recorded. ✓
- `raw_extraction` rows with `extractor_version` → Task 6. ✓
- `email.status` advances `new → classified → extracted`; per-source failure isolated, email → `error` with detail; nothing dropped silently → Task 6. ✓
- `worker.advance_pipeline()` becomes real → Task 6. ✓
- Canonical schema: header + line items, layered deductions, multi-payment (one payload per payment), optional UTR → Task 1 (from the Q&A after reviewing real samples). ✓
- Deferred by design: normalization (raw → canonical via LLM) is the next plan; `vendor` table population is later; a real scanned fixture is pending.

**2. Placeholder scan:** Tasks 3–6 give rules + interfaces + concrete fixture-driven test assertions rather than full verbatim code for the extractor bodies — deliberate, because the extractors are short and the fixture content pins them exactly (every test names a real invoice number / amount / UTR from a known fixture). Task 1 and the schema are fully verbatim. No "TBD" / "handle errors" / "similar to Task N".

**3. Type consistency:** `SourceSpec(kind, ref, skipped, skip_reason)` defined Task 3, consumed Task 6. `ExtractedContent(text, tables, meta)` + `.to_payload()` + `EXTRACTOR_VERSION` defined Task 4, used by Task 5 (`vision.py` returns the same `ExtractedContent`) and Task 6 (dispatch + `raw_extraction` insert). `VisionExtractor.extract_image(data, media_type) -> ExtractedContent` consistent Task 5 ↔ Task 6. `AdvanceStats(classified, extracted, errored)` Task 6. `load_email(name, session, blob_store) -> Email` Task 2, used by Tasks 3 and 6 tests. Schema names (`Deduction`, `DeductionType`, new `Header`/`LineItem`/`Envelope` fields) Task 1, referenced nowhere else in this plan (the normalizer in the next plan produces them).

## Execution Handoff

Handled in chat after the plan is saved.
