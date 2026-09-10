# Journey Dashboard + Threshold-Gated Auto-Send — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the client the full email→JSON journey. Build threshold-gated auto-send (a flag-free, high-confidence extraction skips review and delivers itself), a **Journey** dashboard listing every sampled email and where it is (received → auto-sent / awaiting review / rejected / error), a read-only JSON view for any extraction, and a browsable stub-backend index.

**Architecture:** A shared `ar_pipeline/pipeline/routing.py` holds "approve an extraction → queue a delivery" and "settle an email"; both the human review path and the new auto path call it. `normalize_one` auto-approves eligible rows right after it writes them. The review UI's landing page (`GET /review`) becomes the Journey dashboard; the pending-review list moves to `GET /review/queue`. The offline stub LLM sets `confidence` from how complete its parse was, so the sample set splits into an auto-sent group and a review group with no per-email config.

**Tech Stack:** Python 3.12, FastAPI + Jinja2, SQLAlchemy 2.0 sync, pytest + embedded `pgserver`, `httpx` TestClient.

**Spec:** `docs/superpowers/specs/2026-09-09-ar-email-extraction-design.md` — the `### review/`, `### normalize/`, `### deliver/`, `### stub_backend/` sections and the **"Amendments during implementation (Journey / auto-send plan, 2026-09-11)"** block.

## Global Constraints

- Python `>=3.12`; `uv` for everything (`uv run <cmd>`). No new dependencies.
- SQLAlchemy 2.0 **sync only**. `pipeline/routing.py` and `review/service.py` functions take `session: Session` first and **never commit** — they `flush()`. `normalize_one` also never commits (`advance_once` owns the transaction).
- `MutableDict` JSONB columns: mutate only via **top-level key reassignment** (`row.canonical["envelope"] = {**env, ...}`), never a nested in-place mutation — nested mutations are not flushed. (Same rule the `reviewed_by` / `extraction_id` stamps already follow.)
- Canonical data is the `RemittancePayload` model in `ar_pipeline/schema/canonical.py`. `extraction.status` ∈ `{pending_review, approved, rejected, superseded}`, `delivery.status` ∈ `{pending, delivered, failed}` — **no schema change, no migration** in this plan.
- Auto-approve sentinel: `extraction.reviewed_by = "auto"` (the human path uses the reviewer's typed name; `"auto"` is reserved).
- Server-rendered Jinja2 + plain HTML, autoescape on — never `|safe` on data. Route declaration order in `app.py` is load-bearing: every static-prefix route (`/login`, `/queue`, `/errors`, `/extraction/...`, `/deliveries/...`) is declared **before** the `/{extraction_id}` routes.
- Gate after every task: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest -q` — all clean. Baseline before this plan: **269 passed, 2 deselected**.
- Commit trailer on every commit, exactly and only:
  `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`
  (Do NOT write your own model name. `git log -1 --format=%B` after each commit; `git commit --amend` any wrong trailer.)
- Ruff lint selects `E,F,I,UP,B,SIM`; line length 100.

---

## File Structure

**New:**

| File | Responsibility |
|---|---|
| `ar_pipeline/pipeline/routing.py` | `approve_and_queue(session, extraction, *, reviewed_by)`, `settle_email(session, email)`, `AUTO_REVIEWER = "auto"` |
| `ar_pipeline/review/templates/journey.html` | the dashboard |
| `ar_pipeline/review/templates/extraction.html` | read-only single-extraction view (JSON + journey metadata) |
| `stub_backend/templates/index.html` | *(optional — inline HTML string is fine; see Task 6)* |
| `tests/pipeline/test_routing.py`, `tests/review/test_journey.py`, `tests/review/test_extraction_view.py`, `tests/normalize/test_auto_approve.py`, `tests/stub_backend/test_index.py` | tests |

**Modified:**

| File | Change |
|---|---|
| `ar_pipeline/config.py` | `auto_approve_min_confidence: float = 0.0` |
| `ar_pipeline/normalize/service.py` | after the flush, auto-approve eligible rows; email → `done`/`review` accordingly |
| `ar_pipeline/normalize/stub_client.py` | `_draft` computes `confidence` from parse completeness |
| `ar_pipeline/review/service.py` | `approve_extraction`/`reject_extraction` use `routing.settle_email` (drop local `_close_email_if_done`); add `list_journey()`, `load_extraction_view()` |
| `ar_pipeline/review/app.py` | `GET /review` → journey; new `GET /review/queue`, `GET /review/extraction/{id}` |
| `ar_pipeline/review/templates/base.html` | nav: Journey · Queue · Errors |
| `ar_pipeline/review/templates/queue.html` | heading tweak + "view JSON" links |
| `stub_backend/app.py` + `stub_backend/store.py` | `GET /` + `GET /remittances` list |
| `scripts/setup` | seed `AUTO_APPROVE_MIN_CONFIDENCE=0.75` into `.env` |
| `.env.example` | add `AUTO_APPROVE_MIN_CONFIDENCE=` with a comment |
| `README.md` | demo walkthrough shows the journey + both paths |
| `tests/review/test_routes_queue.py` (and any other test hitting `GET /review` for queue content) | point at `/review/queue` |

---

## Task 1: `pipeline/routing.py` — shared approve-and-queue + settle-email

**Files:**
- Create: `ar_pipeline/pipeline/routing.py`
- Create: `tests/pipeline/test_routing.py`
- Modify: `ar_pipeline/review/service.py` (`approve_extraction`, `reject_extraction`, delete `_close_email_if_done`)
- Modify: `tests/review/test_service.py` (imports only if a test referenced `_close_email_if_done` — it does not; verify)

**Interfaces:**
- Consumes: `ar_pipeline.db.models` (`Email`, `Extraction`, `Delivery`).
- Produces:
  - `AUTO_REVIEWER: str = "auto"`
  - `def approve_and_queue(session: Session, extraction: Extraction, *, reviewed_by: str) -> None` — set `extraction.status = "approved"`, `extraction.reviewed_by = reviewed_by`, `extraction.reviewed_at = func.now()`; if `extraction.canonical` is a dict with a dict `"envelope"`, `extraction.canonical["envelope"] = {**env, "reviewed_by": reviewed_by}`; `session.add(Delivery(extraction_id=extraction.id, status="pending", next_attempt_at=func.now()))`; `session.flush()`. Assumes the caller already checked the extraction is approvable.
  - `def settle_email(session: Session, email: Email) -> None` — `email.status = "done"` iff no `Extraction` for `email.id` has `status == "pending_review"`; `session.flush()`.

- [ ] **Step 1: Write the failing tests**

`tests/pipeline/test_routing.py`:
```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from ar_pipeline.db.models import Delivery, Email, Extraction
from ar_pipeline.pipeline.routing import AUTO_REVIEWER, approve_and_queue, settle_email


def _email(db_session, status="review") -> Email:
    e = Email(
        internet_message_id=f"m-{uuid.uuid4()}", sender_address="a@b.com",
        sender_domain="b.com", subject="s", received_at=datetime(2026, 9, 1, tzinfo=UTC),
        status=status,
    )
    db_session.add(e)
    db_session.flush()
    return e


def _ext(db_session, email, *, status="pending_review", canonical=None) -> Extraction:
    x = Extraction(
        email_id=email.id, status=status, is_remittance=True,
        canonical=canonical if canonical is not None else {"envelope": {"extraction_id": "x"}},
        confidence=Decimal("0.9"),
    )
    db_session.add(x)
    db_session.flush()
    return x


def test_approve_and_queue_sets_status_stamps_reviewer_and_inserts_delivery(db_session):
    email = _email(db_session)
    ext = _ext(db_session, email)
    approve_and_queue(db_session, ext, reviewed_by=AUTO_REVIEWER)
    db_session.refresh(ext)
    assert ext.status == "approved"
    assert ext.reviewed_by == "auto"
    assert ext.canonical["envelope"]["reviewed_by"] == "auto"
    deliveries = db_session.scalars(select(Delivery).where(Delivery.extraction_id == ext.id)).all()
    assert len(deliveries) == 1 and deliveries[0].status == "pending"


def test_approve_and_queue_tolerates_empty_canonical(db_session):
    email = _email(db_session)
    ext = _ext(db_session, email, canonical={})
    approve_and_queue(db_session, ext, reviewed_by="Asha")  # no envelope -> no crash
    db_session.refresh(ext)
    assert ext.status == "approved"


def test_settle_email_marks_done_when_nothing_pending(db_session):
    email = _email(db_session)
    _ext(db_session, email, status="approved")
    settle_email(db_session, email)
    db_session.refresh(email)
    assert email.status == "done"


def test_settle_email_leaves_review_when_a_pending_row_remains(db_session):
    email = _email(db_session)
    _ext(db_session, email, status="approved")
    _ext(db_session, email, status="pending_review")
    settle_email(db_session, email)
    db_session.refresh(email)
    assert email.status == "review"
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/pipeline/test_routing.py -q` → `ModuleNotFoundError`.

- [ ] **Step 3: Implement `routing.py`**

```python
"""Shared post-review routing: approve an extraction and queue its delivery,
and mark an email done once nothing is left to review.

Both the human review path (`review/service.approve_extraction`) and the
auto-send path (`normalize/service.normalize_one`) call these, so the two
never drift.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Delivery, Email, Extraction

AUTO_REVIEWER = "auto"


def approve_and_queue(session: Session, extraction: Extraction, *, reviewed_by: str) -> None:
    """Approve `extraction` and insert a pending `delivery`. The caller has
    already checked the extraction is approvable."""
    extraction.status = "approved"
    extraction.reviewed_by = reviewed_by
    extraction.reviewed_at = func.now()
    canonical = extraction.canonical
    if isinstance(canonical, dict):
        env = canonical.get("envelope")
        if isinstance(env, dict):
            # top-level reassignment so MutableDict tracks it
            extraction.canonical["envelope"] = {**env, "reviewed_by": reviewed_by}
    session.add(
        Delivery(extraction_id=extraction.id, status="pending", next_attempt_at=func.now())
    )
    session.flush()


def settle_email(session: Session, email: Email) -> None:
    """`email` -> `done` once no extraction for it is still `pending_review`."""
    pending = session.scalar(
        select(func.count())
        .select_from(Extraction)
        .where(Extraction.email_id == email.id, Extraction.status == "pending_review")
    )
    if not pending:
        email.status = "done"
    session.flush()
```

- [ ] **Step 4: Refactor `review/service.py`**

- Delete `_close_email_if_done` (lines ~117-125).
- In `approve_extraction`: replace the body that sets status / reviewed_by / reviewed_at / envelope stamp / `Delivery(...)` insert with `approve_and_queue(session, ext, reviewed_by=user.name)`, then `settle_email(session, email)`. Keep the `_require_pending` + `_check_approvable(ext)` guards before it.
- In `reject_extraction`: replace the `_close_email_if_done(session, email)` call with `settle_email(session, email)`.
- Add `from ar_pipeline.pipeline.routing import approve_and_queue, settle_email` to the imports.
- `_check_approvable` stays (still called by `approve_extraction` and `save_edits`).

- [ ] **Step 5: Run all affected tests**

`uv run pytest tests/pipeline/test_routing.py tests/review/test_service.py tests/review/test_routes_actions.py tests/review/test_routes_deliveries.py tests/review/test_delivery_integration.py -q` → all pass (the existing approve/reject behavior is unchanged — `test_approve_sets_status_and_inserts_delivery`, `test_reject_requires_reason`, the e2e `reviewed_by`/`envelope.reviewed_by` assertions must still hold).

- [ ] **Step 6: Gate + commit**

```bash
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest -q
git add ar_pipeline/pipeline/routing.py ar_pipeline/review/service.py tests/pipeline/test_routing.py
git commit -m "refactor: shared pipeline/routing (approve_and_queue, settle_email)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: auto-approve eligible extractions in `normalize_one`

**Files:**
- Modify: `ar_pipeline/config.py` (`auto_approve_min_confidence`)
- Modify: `ar_pipeline/normalize/service.py`
- Create: `tests/normalize/test_auto_approve.py`

**Interfaces:**
- Consumes: `ar_pipeline.pipeline.routing.approve_and_queue` / `AUTO_REVIEWER`, `ar_pipeline.config.get_settings`.
- Produces: no new callable — `normalize_one` gains the auto-approve step and its return value is unchanged (count of rows added).

**Behavior:** after `normalize_one` writes its `Extraction` rows, flushes, and stamps `envelope.extraction_id`:
- `threshold = get_settings().auto_approve_min_confidence`.
- If `threshold > 0`: for each row in `rows`, if `row.is_remittance` AND `row.canonical` (truthy) AND `not row.validation_flags` AND `row.confidence is not None` AND `row.confidence >= threshold` → `approve_and_queue(session, row, reviewed_by=AUTO_REVIEWER)`.
- Then set `email.status`: `"review"` if any row is still `pending_review`, else `"done"`. (Use a count query, or check `all(r.status != "pending_review" for r in rows)` — but a row auto-approved above is in the session, so the in-memory check is fine. The empty-`else`-branch case — one non-remittance row — keeps `"review"` as today.)
- `session.flush()`.

Keep it simple: the existing code path that sets `email.status = "review"` unconditionally stays; the auto-approve loop runs after it and then a final `email.status = "done" if <no pending rows> else "review"`.

- [ ] **Step 1: Add the config field**

`ar_pipeline/config.py`, after `llm_model`:
```python
    auto_approve_min_confidence: float = 0.0
```

- [ ] **Step 2: Write the failing tests**

`tests/normalize/test_auto_approve.py`:
```python
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from ar_pipeline.db.models import Delivery, Email, Extraction, ExtractionSource, RawExtraction
from ar_pipeline.normalize.normalizer import NormalizerOutput, PaymentDraft
from ar_pipeline.normalize.service import normalize_one
from ar_pipeline.schema.canonical import LineItem
from tests.normalize.llm_fake import FakeLLMClient


def _extracted_email(db_session, mid: str) -> Email:
    email = Email(
        internet_message_id=mid, sender_address="a@b.com", sender_domain="b.com",
        subject="s", received_at=__import__("datetime").datetime(2026, 9, 1, tzinfo=__import__("datetime").UTC),
        status="extracted",
    )
    db_session.add(email)
    db_session.flush()
    src = ExtractionSource(email_id=email.id, kind="body_text", ref="body")
    db_session.add(src)
    db_session.flush()
    db_session.add(RawExtraction(extraction_source_id=src.id, payload={"text": "advice", "tables": []}))
    db_session.flush()
    return email


def _clean_output(conf: float) -> NormalizerOutput:
    return NormalizerOutput(
        is_remittance=True,
        payments=[PaymentDraft(
            payer_name="Acme", total_paid_amount=Decimal("100.00"),
            line_items=[LineItem(invoice_number="INV-1", invoice_amount=Decimal("100.00"),
                                 amount_paid=Decimal("100.00"))],
            confidence=conf,
        )],
    )


def test_high_confidence_flag_free_extraction_is_auto_approved(db_session, monkeypatch):
    monkeypatch.setattr("ar_pipeline.config.get_settings",
                        lambda: __import__("ar_pipeline.config", fromlist=["Settings"]).Settings(
                            database_url="x", auto_approve_min_confidence=0.75))
    email = _extracted_email(db_session, "m-auto-1")
    normalize_one(db_session, email, FakeLLMClient(response=_clean_output(0.9)))
    db_session.refresh(email)
    ext = db_session.scalars(select(Extraction).where(Extraction.email_id == email.id)).one()
    assert ext.status == "approved"
    assert ext.reviewed_by == "auto"
    assert email.status == "done"
    assert db_session.scalars(select(Delivery).where(Delivery.extraction_id == ext.id)).all()


def test_low_confidence_extraction_still_goes_to_review(db_session, monkeypatch):
    monkeypatch.setattr("ar_pipeline.config.get_settings",
                        lambda: __import__("ar_pipeline.config", fromlist=["Settings"]).Settings(
                            database_url="x", auto_approve_min_confidence=0.75))
    email = _extracted_email(db_session, "m-auto-2")
    normalize_one(db_session, email, FakeLLMClient(response=_clean_output(0.3)))
    db_session.refresh(email)
    ext = db_session.scalars(select(Extraction).where(Extraction.email_id == email.id)).one()
    assert ext.status == "pending_review"
    assert email.status == "review"


def test_threshold_zero_keeps_everything_in_review(db_session, monkeypatch):
    # default Settings() -> auto_approve_min_confidence == 0.0
    email = _extracted_email(db_session, "m-auto-3")
    normalize_one(db_session, email, FakeLLMClient(response=_clean_output(0.99)))
    db_session.refresh(email)
    ext = db_session.scalars(select(Extraction).where(Extraction.email_id == email.id)).one()
    assert ext.status == "pending_review"
```

**Note for the implementer:** the `monkeypatch` of `get_settings` above is illustrative — use whatever mechanism the existing `tests/normalize/` suite uses for settings overrides (check `test_service.py`; it may just rely on the conftest DB env + `monkeypatch.setenv("AUTO_APPROVE_MIN_CONFIDENCE", "0.75")` + `get_settings.cache_clear()`). Pick the one that works; the assertions are the contract. `normalize_one` reads `get_settings()` itself, so the override must be visible there.

- [ ] **Step 3: Run to verify failure** — the clean 0.9 extraction is `pending_review`, not `approved`.

- [ ] **Step 4: Implement**

In `ar_pipeline/normalize/service.py`, add the import:
```python
from ar_pipeline.pipeline.routing import AUTO_REVIEWER, approve_and_queue
```
Replace the tail of `normalize_one` (from `email.status = "review"` onward):
```python
    email.status = "review"
    session.flush()
    # stamp the real PK into the canonical envelope (was a placeholder uuid)
    for row in rows:
        env = row.canonical.get("envelope")
        if isinstance(env, dict):
            row.canonical["envelope"] = {**env, "extraction_id": str(row.id)}
    if rows:
        session.flush()

    threshold = get_settings().auto_approve_min_confidence
    if threshold > 0:
        for row in rows:
            if (
                row.is_remittance
                and row.canonical
                and not row.validation_flags
                and row.confidence is not None
                and float(row.confidence) >= threshold
            ):
                approve_and_queue(session, row, reviewed_by=AUTO_REVIEWER)
        if all(r.status != "pending_review" for r in rows) and rows:
            email.status = "done"
        session.flush()
    return added
```
(`row.confidence` is a `Decimal | None`; `float(...)` for the compare.)

- [ ] **Step 5: Run + gate + commit**

```bash
uv run pytest tests/normalize -q     # all green incl. the 3 new
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest -q
git add ar_pipeline/config.py ar_pipeline/normalize/service.py tests/normalize/test_auto_approve.py
git commit -m "feat: threshold-gated auto-approve in normalize_one

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 3: stub LLM — confidence from parse completeness

**Files:**
- Modify: `ar_pipeline/normalize/stub_client.py` (`_draft`)
- Modify: `tests/normalize/test_stub_client.py` (the `confidence == 0.15` assertions)

**Interfaces:** no signature change. `_draft` still returns the same dict shape; only the `confidence` value changes.

**Behavior:** `_draft` computes `confidence` = `0.2` base, plus `0.25` if an amount was found (`top > 0`), `0.2` if a bank reference was found, `0.2` if an invoice-number token was found, `0.1` if `top > 0` (the identity trivially reconciles — always true when there's an amount, so fold it: `0.25 + 0.1` for an amount). Clamp to `0.95`. So:
- nothing found → `0.2`
- amount only → `0.55`
- amount + reference → `0.75`
- amount + reference + invoice number → `0.95`

With `auto_approve_min_confidence = 0.75`, an email where the stub finds an amount + a reference (or + invoice no.) auto-sends; a partial one goes to review. The "empty invoice number" validation flag still blocks auto-send on its own when there's no invoice token, which is the belt-and-suspenders check — good.

- [ ] **Step 1: Update the failing assertions**

In `tests/normalize/test_stub_client.py`, `test_returns_one_low_confidence_remittance` asserts `p.confidence == 0.15` — change to `assert 0.0 < p.confidence <= 0.95` (it's now derived). Add:
```python
def test_confidence_reflects_how_much_was_found():
    bare = _parse("Subject: hi\n\nplease see attached")
    rich = _parse("NEFT ref SBIN225551234567 — INV-2026-9 — total 1,23,456.78")
    assert bare.payments[0].confidence < 0.5
    assert rich.payments[0].confidence >= 0.9
```

- [ ] **Step 2: Run to verify failure** — `test_confidence_reflects_how_much_was_found` fails (flat 0.15).

- [ ] **Step 3: Implement** — in `_draft`, replace `"confidence": 0.15` with a computed value:
```python
    invoice_number = _invoice_number(text)
    confidence = 0.2
    if top > 0:
        confidence += 0.35
    if reference:
        confidence += 0.2
    if invoice_number:
        confidence += 0.2
    confidence = min(confidence, 0.95)
    return {
        ...
        "line_items": [{"invoice_number": invoice_number, "invoice_amount": top, "amount_paid": top}],
        "confidence": round(confidence, 2),
    }
```
(Compute `invoice_number` once; the current code calls `_invoice_number(text)` inline — hoist it.)

- [ ] **Step 4: Run + gate + commit**

```bash
uv run pytest tests/normalize/test_stub_client.py -q
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest -q
git add ar_pipeline/normalize/stub_client.py tests/normalize/test_stub_client.py
git commit -m "feat: stub LLM confidence reflects parse completeness

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: Journey dashboard — `GET /review`, queue moves to `/review/queue`

**Files:**
- Modify: `ar_pipeline/review/service.py` (`list_journey`)
- Modify: `ar_pipeline/review/app.py` (`GET /review` → journey; `GET /review/queue` → the old queue body)
- Create: `ar_pipeline/review/templates/journey.html`
- Modify: `ar_pipeline/review/templates/base.html` (nav)
- Modify: `ar_pipeline/review/templates/queue.html` (heading + a "← Journey" link)
- Create: `tests/review/test_journey.py`
- Modify: `tests/review/test_routes_queue.py` and any other test that does `client.get("/review")` expecting queue content → `client.get("/review/queue")`

**Interfaces:**
- Produces in `service.py`:
  - `@dataclass(frozen=True) class JourneyExtraction`: `id: uuid.UUID`, `payment_index: int`, `outcome: str` (one of `"auto-approved"`, `"awaiting review"`, `"approved"`, `"rejected"`, `"superseded"`, `"not a remittance"`), `confidence: Decimal | None`, `flag_count: int`, `delivery: str` (`"delivered"` / `"pending"` / `"failed"` / `"—"`)
  - `@dataclass(frozen=True) class JourneyRow`: `email_id: uuid.UUID`, `subject: str`, `sender_address: str`, `received_at: datetime`, `attachment_count: int`, `stage: str` (friendly: `"Received"` / `"Classified"` / `"Extracted"` / `"Awaiting review"` / `"Complete"` / `"Error"`), `error_detail: str | None`, `extractions: list[JourneyExtraction]`
  - `def list_journey(session: Session) -> list[JourneyRow]` — all emails, newest `received_at` first. One query for emails + `attachment` counts, one for extractions, one for `delivery` rows keyed by `extraction_id`; assemble in Python (≤50 emails/day — fine).
- Produces routes:
  - `GET /review` → `journey.html` with `rows=list_journey(session)`
  - `GET /review/queue` → `queue.html` (the exact body `GET /review` had — `rows=list_pending(session)`)

**Outcome mapping** (`JourneyExtraction.outcome`): `status=="approved"` and `reviewed_by=="auto"` → `"auto-approved"`; `status=="approved"` → `"approved"`; `status=="pending_review"` and (`not is_remittance` or `not canonical`) → `"not a remittance"`; `status=="pending_review"` → `"awaiting review"`; `status=="rejected"` → `"rejected"`; `status=="superseded"` → `"superseded"`.

**Stage mapping** (`JourneyRow.stage` from `email.status`): `new`→`"Received"`, `classified`→`"Classified"`, `extracted`→`"Extracted"`, `normalized`→`"Normalizing"`, `review`→`"Awaiting review"`, `done`→`"Complete"`, `error`→`"Error"`.

- [ ] **Step 1: Write the failing tests**

`tests/review/test_journey.py`:
```python
import pytest
from fastapi.testclient import TestClient

from ar_pipeline.main import app


@pytest.fixture
def client(db_session):
    from ar_pipeline.review.app import get_db

    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app, follow_redirects=False) as c:
        c.post("/review/login", data={"password": "test-shared-secret", "name": "Asha"})
        yield c
    app.dependency_overrides.clear()


def test_review_root_is_the_journey(client, seed_pending):
    email, ext = seed_pending()
    r = client.get("/review")
    assert r.status_code == 200
    assert "Journey" in r.text
    assert email.subject in r.text
    # a link to the read-only JSON view for that extraction
    assert f"/review/extraction/{ext.id}" in r.text


def test_journey_shows_auto_approved_vs_awaiting(client, db_session, seed_pending):
    from ar_pipeline.db.models import Delivery

    e1, x1 = seed_pending()                       # pending -> "awaiting review"
    e2, x2 = seed_pending()
    x2.status = "approved"
    x2.reviewed_by = "auto"
    db_session.add(Delivery(extraction_id=x2.id, status="delivered"))
    db_session.flush()
    text = client.get("/review").text
    assert "awaiting review" in text.lower()
    assert "auto-approved" in text.lower()
    assert "delivered" in text.lower()


def test_queue_moved_to_review_queue(client, seed_pending):
    email, ext = seed_pending()
    assert client.get("/review/queue").status_code == 200
    assert email.subject in client.get("/review/queue").text


def test_journey_requires_login():
    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get("/review").status_code == 303
```

Update `tests/review/test_routes_queue.py`: every `client.get("/review")` that asserts queue content → `client.get("/review/queue")`. (The badge test, the "lists pending" test.)

- [ ] **Step 2: Run to verify failure** — `GET /review` still returns the queue; no `/review/queue`; no `journey.html`.

- [ ] **Step 3: Implement `list_journey` in `service.py`** (assemble from 3 queries — emails+attachment-count, extractions, deliveries). Keep the dataclasses near `QueueRow`.

- [ ] **Step 4: Rework the routes in `app.py`**

```python
@router.get("", response_class=HTMLResponse)
def journey_page(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.review.service import list_journey

    return _render(
        request, "journey.html",
        user=user, rows=list_journey(session), flash=request.query_params.get("flash"),
    )


@router.get("/queue", response_class=HTMLResponse)
def queue_page(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.review.service import list_pending

    return _render(
        request, "queue.html",
        user=user, rows=list_pending(session), flash=request.query_params.get("flash"),
    )
```
Keep `/queue` declared before the `/{extraction_id}` routes.

- [ ] **Step 5: Templates**

`base.html` nav — replace the current links with:
```html
<a href="/review">Journey</a>
<a href="/review/queue">Queue</a>
<a href="/review/errors">Errors</a>
```

`journey.html` (extends `base.html`) — a table, one row per email, with a nested list of its extractions. Column headers: **Email received** · **Pipeline** · **Extractions** · (per extraction) **Outcome / Confidence / Delivery / JSON**. Wording:
- Title: `<h1>Processing journey <small>— received email → delivered JSON</small></h1>`
- A short lead line: `<p>Every email the mailbox received. Flag-free, high-confidence extractions are approved automatically and sent straight to the backend; the rest wait for a reviewer.</p>`
- Stage as a pill (`<span class="badge">`); outcome as a pill with a class per outcome (`badge ok` for auto-approved/approved/delivered, `badge warn` for awaiting review, `badge bad` for rejected/error/not a remittance).
- Each extraction row links `→ JSON` to `/review/extraction/{{ x.id }}`.
- Empty state: "No emails yet. Run `ar-pipeline ingest-eml …`."

`queue.html` — change the `<h1>` to `Review queue <small>— extractions that need a human</small>` and add `<p><a href="/review">← Full journey</a></p>` at the top.

- [ ] **Step 6: Run + gate + commit**

```bash
uv run pytest tests/review -q
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest -q
git add ar_pipeline/review/service.py ar_pipeline/review/app.py ar_pipeline/review/templates tests/review/test_journey.py tests/review/test_routes_queue.py
git commit -m "feat: Journey dashboard at /review; pending list -> /review/queue

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 5: read-only extraction / JSON view

**Files:**
- Modify: `ar_pipeline/review/service.py` (`load_extraction_view`)
- Modify: `ar_pipeline/review/app.py` (`GET /review/extraction/{extraction_id}`)
- Create: `ar_pipeline/review/templates/extraction.html`
- Create: `tests/review/test_extraction_view.py`

**Interfaces:**
- Produces in `service.py`:
  - `@dataclass(frozen=True) class ExtractionView`: `extraction: Extraction`, `email: Email`, `outcome: str` (same mapping as `JourneyExtraction.outcome`), `delivery: str`, `canonical_json: str` (`json.dumps(extraction.canonical, indent=2, sort_keys=False, default=str)`)
  - `def load_extraction_view(session: Session, extraction_id: uuid.UUID) -> ExtractionView` — `ReviewError` if not found.
- Produces route: `GET /review/extraction/{extraction_id}` — `?format=json` → `Response(view.canonical_json, media_type="application/json")`; otherwise `extraction.html`. Behind `require_user`. **Declared before `/{extraction_id}`** (so `/review/extraction/<uuid>` isn't captured by the edit-page route — actually `extraction` is a static segment and `/{extraction_id}` is a `uuid.UUID` path param, so `/review/extraction` won't match `/{extraction_id}`; still, declare it earlier for clarity and to be safe against future changes).

- [ ] **Step 1: Write the failing tests**

`tests/review/test_extraction_view.py`:
```python
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from ar_pipeline.main import app


@pytest.fixture
def client(db_session):
    from ar_pipeline.review.app import get_db

    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app, follow_redirects=False) as c:
        c.post("/review/login", data={"password": "test-shared-secret", "name": "Asha"})
        yield c
    app.dependency_overrides.clear()


def test_extraction_page_renders_pretty_json_for_any_status(client, db_session, seed_pending):
    email, ext = seed_pending()
    ext.status = "approved"
    ext.reviewed_by = "auto"
    db_session.flush()
    r = client.get(f"/review/extraction/{ext.id}")
    assert r.status_code == 200
    assert "auto-approved" in r.text.lower()
    assert ext.canonical["header"]["payer_name"] in r.text


def test_extraction_raw_json_endpoint(client, seed_pending):
    email, ext = seed_pending()
    r = client.get(f"/review/extraction/{ext.id}?format=json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = json.loads(r.text)
    assert body["envelope"]["extraction_id"]


def test_extraction_view_unknown_id_404(client):
    assert client.get(f"/review/extraction/{uuid.uuid4()}").status_code == 404


def test_extraction_view_requires_login():
    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get(f"/review/extraction/{uuid.uuid4()}").status_code == 303
```

- [ ] **Step 2: Run to verify failure** — route 404s (not found as in no-route).

- [ ] **Step 3: Implement** `load_extraction_view` + the route (`ReviewError` → `HTTPException(404)`; `?format=json` branch).

- [ ] **Step 4: `extraction.html`** — extends `base.html`. Header block: email subject, sender, received time; the outcome pill; confidence; flag list; delivery status. Then `<h2>Canonical JSON</h2>` + `<pre class="raw">{{ view.canonical_json }}</pre>` (autoescaped). A link `<a href="?format=json">raw</a>`. A "← Journey" link. If the extraction is `pending_review`, also show `<a href="/review/{{ view.extraction.id }}">Open in review editor</a>`.

- [ ] **Step 5: Link it from the journey + queue**

`journey.html` already links `→ JSON`. In `queue.html`, in each row add `<a href="/review/extraction/{{ r.extraction_id }}">JSON</a>` next to the "Open" link.

- [ ] **Step 6: Run + gate + commit**

```bash
uv run pytest tests/review -q
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest -q
git add ar_pipeline/review/service.py ar_pipeline/review/app.py ar_pipeline/review/templates/extraction.html ar_pipeline/review/templates/queue.html tests/review/test_extraction_view.py
git commit -m "feat: read-only extraction + raw-JSON view for any extraction

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 6: browsable stub backend + demo wiring + end-to-end

**Files:**
- Modify: `stub_backend/store.py` (a list helper)
- Modify: `stub_backend/app.py` (`GET /`, `GET /remittances`)
- Modify: `scripts/setup` (seed `AUTO_APPROVE_MIN_CONFIDENCE=0.75`)
- Modify: `.env.example`
- Modify: `README.md`
- Create: `tests/stub_backend/test_index.py`
- Create: `tests/review/test_journey_end_to_end.py`

**Interfaces:**
- `stub_backend/store.py`: add `def received_list() -> list[dict]: return list(RECEIVED.values())`.
- `stub_backend/app.py`:
  - `GET /remittances` → `{"count": len(RECEIVED), "remittances": received_list()}`
  - `GET /` → an HTML page (inline f-string template is fine — this is a stub): a heading "AR stub backend — received remittances (N)" and a `<table>` of `extraction_id`, `payer_name`, `total_paid_amount`, `payment_reference`, `#line_items`, plus each row's JSON in a `<details><pre>`. Autoescape isn't available in a raw f-string — use `html.escape()` on every interpolated value.

- [ ] **Step 1: Write the failing tests**

`tests/stub_backend/test_index.py`:
```python
from fastapi.testclient import TestClient

from stub_backend.app import app
from stub_backend.store import RECEIVED, _IDEMPOTENCY


def _payload(eid="ext-1"):
    return {
        "envelope": {"extraction_id": eid, "source_email_id": "e", "payment_index": 0,
                     "vendor_guess": None, "extracted_at": "2026-09-01T00:00:00Z", "reviewed_by": "auto"},
        "header": {"payer_name": "Acme", "payer_id": None, "payment_reference": "UTR9",
                   "payment_reference_type": "utr", "payment_date": None, "payment_method": None,
                   "currency": "INR", "total_paid_amount": "100.00", "deductions": []},
        "line_items": [{"invoice_number": "INV-1", "invoice_date": None, "invoice_amount": "100.00",
                        "deductions": [], "amount_paid": "100.00"}],
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
```

`tests/review/test_journey_end_to_end.py` — the money test:
```python
"""Ingest several fixtures with LLM_PROVIDER=stub + a 0.75 auto-approve
threshold; some auto-send, some land in the review queue; the journey shows
both, and the stub backend received the auto-sent ones."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ar_pipeline.db.models import Delivery, Email, Extraction
from ar_pipeline.deliver.backend_client import BackendClient
from ar_pipeline.deliver.deliverer import run_deliveries
from ar_pipeline.main import app
from ar_pipeline.storage import LocalBlobStore
from stub_backend.app import app as stub_app
from stub_backend.store import RECEIVED, _IDEMPOTENCY
from tests.extract.vision_fake import FakeVisionExtractor
from tests.fixtures.loader import FIXTURE_NAMES, load_email
from tests.normalize.llm_fake import FakeLLMClient  # NOT used — see note


@pytest.fixture
def client(db_session):
    from ar_pipeline.review.app import get_db

    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app, follow_redirects=False) as c:
        c.post("/review/login", data={"password": "test-shared-secret", "name": "Asha"})
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _stub_clear():
    RECEIVED.clear(); _IDEMPOTENCY.clear()
    yield
    RECEIVED.clear(); _IDEMPOTENCY.clear()


def test_end_to_end_journey_split(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_APPROVE_MIN_CONFIDENCE", "0.75")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    import ar_pipeline.config as cfg
    cfg.get_settings.cache_clear()

    from ar_pipeline.normalize.llm_client import get_llm_client
    from ar_pipeline.pipeline.advance import advance_once

    store = LocalBlobStore(str(tmp_path))
    vision = FakeVisionExtractor()
    for name in FIXTURE_NAMES:
        load_email(name, db_session, store)
    for _ in range(3):
        advance_once(db_session, store, vision, get_llm_client())

    exts = db_session.scalars(select(Extraction)).all()
    auto = [x for x in exts if x.status == "approved" and x.reviewed_by == "auto"]
    pend = [x for x in exts if x.status == "pending_review"]
    assert auto and pend, f"expected a split; auto={len(auto)} pending={len(pend)}"

    # the journey page shows both
    text = client.get("/review").text
    assert "auto-approved" in text.lower() and "awaiting review" in text.lower()

    # deliver the auto-approved ones to the stub
    backend = BackendClient(base_url="http://stub",
                            http=TestClient(stub_app))  # TestClient is an httpx.Client
    run_deliveries(db_session, backend)
    assert len(RECEIVED) == len(auto)
    cfg.get_settings.cache_clear()
```
**Implementer note:** confirm `FakeLLMClient` import is unused and drop it; the test deliberately uses the *real* `get_llm_client()` which returns the stub because `LLM_PROVIDER=stub`. If `load_email` / `advance_once` read settings that the `monkeypatch.setenv` + `cache_clear()` doesn't reach, adjust — the assertion (a genuine auto/pending split from the 6 fixtures) is the contract. If the 6 committed fixtures happen not to split at 0.75, tune the threshold in the test (e.g. 0.7) and in `scripts/setup` to match, or add one more obviously-complete fixture — but first check what confidences the fixtures actually produce.

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** the stub backend routes + `scripts/setup` seed (add `AUTO_APPROVE_MIN_CONFIDENCE` to the `fill_if_blank` dict with value `"0.75"`) + `.env.example` line:
```
# Phase 2 auto-send: extractions with no flags and confidence >= this skip
# review and deliver automatically. 0 = review everything (Phase 1).
AUTO_APPROVE_MIN_CONFIDENCE=0.75
```

- [ ] **Step 4: README** — in the demo walkthrough, after `ar-pipeline tick`, add:
```
Open http://localhost:8000/review — the **Journey**. Some emails are
already **Auto-approved & delivered** (flag-free, high confidence); others
are **Awaiting review**. Click any row's JSON link to see the canonical
payload. Open http://localhost:9000/ to see what the backend received.
```
and note `AUTO_APPROVE_MIN_CONFIDENCE` in the env description.

- [ ] **Step 5: Run + gate + commit**

```bash
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest -q
git add stub_backend tests/stub_backend/test_index.py tests/review/test_journey_end_to_end.py scripts/setup .env.example README.md
git commit -m "feat: browsable stub backend + journey end-to-end demo wiring

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage** (the 2026-09-11 amendment):

| Requirement | Task |
|---|---|
| `Settings.auto_approve_min_confidence`, `0.0` = Phase 1 | Task 2 |
| `pipeline/routing.py` shared `approve_and_queue` + `settle_email`, both paths call it | Task 1 |
| `normalize_one` auto-approves flag-free + high-confidence + is_remittance rows, `reviewed_by="auto"` | Task 2 |
| email → `done` if all auto, else `review` | Task 2 |
| stub LLM confidence from parse completeness → sample set splits | Task 3 |
| `GET /review` = Journey; pending list → `GET /review/queue`; nav Journey·Queue·Errors | Task 4 |
| journey row: received → stage → per-email outcome → delivery | Task 4 |
| `GET /review/extraction/{id}` read-only for any status; `?format=json` raw | Task 5 |
| stub_backend `GET /` (HTML) + `GET /remittances` (JSON list) | Task 6 |
| no migration | (constraint; `approved` status + `reviewed_by` sentinel already exist) |

**2. Placeholder scan:** Task 2 and Task 6 flag settings-override / fixture-confidence details for the implementer to verify against the real suite — each carries the concrete contract (the assertions) so it's a "confirm the mechanism, not the behavior" note, not a TODO. No literal `TBD`.

**3. Type consistency:**
- `approve_and_queue(session, extraction: Extraction, *, reviewed_by: str) -> None`, `settle_email(session, email: Email) -> None`, `AUTO_REVIEWER = "auto"` — Task 1; consumed by `review/service` (Task 1) and `normalize/service` (Task 2).
- `JourneyRow` / `JourneyExtraction` / `list_journey` — Task 4; `ExtractionView` / `load_extraction_view` — Task 5; both consumed only by `app.py` routes in the same task.
- Outcome string vocabulary (`"auto-approved"`, `"awaiting review"`, `"approved"`, `"rejected"`, `"superseded"`, `"not a remittance"`) — defined in Task 4, reused verbatim in Task 5's `ExtractionView.outcome` and its tests.
- `reviewed_by == "auto"` sentinel — Task 1 sets it, Tasks 4/5 branch on it, Task 6's e2e asserts it.
- Route order: `/queue`, `/errors`, `/extraction/{id}`, `/deliveries/{id}/resend` all before `/{extraction_id}` — Tasks 4, 5 (and pre-existing).
