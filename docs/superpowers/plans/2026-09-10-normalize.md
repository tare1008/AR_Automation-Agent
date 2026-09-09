# Normalize Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For each email in `status="extracted"`, feed its `raw_extraction` rows to an LLM that produces one or more canonical `RemittancePayload`s (one per payment), run deterministic validation, write `extraction` rows (`status="pending_review"`), and advance the email to `review`.

**Architecture:** A swappable `LLMClient` (Anthropic implementation + a fake). `normalize_email()` builds one prompt from all of an email's raw extractions + sender context, calls the LLM for structured output (a `NormalizerOutput` wrapper), fills the `Envelope` we own, validates each payload with deterministic checks, and returns packaged results. `run_normalize()` persists them as `extraction` rows and flips the email to `review`, per-email isolation, commit per email. Wired as the third step of `advance_pipeline()`.

**Tech Stack:** Python 3.12, `anthropic` SDK (`messages.parse` structured output), Pydantic 2, SQLAlchemy 2.0 (sync), pytest.

**Spec:** `docs/superpowers/specs/2026-09-09-ar-email-extraction-design.md` (`normalize/` module, the error-handling table, and both amendment blocks)

**Builds on:** Foundation + Ingestion + Classify&Extract (all merged to `master`, `82d4a42`). Available:
- `ar_pipeline.schema.canonical`: `RemittancePayload`, `Envelope`, `Header`, `LineItem`, `Deduction`, `DeductionType`, `CANONICAL_JSON_SCHEMA`. **Sign convention:** every `Deduction.amount` is `Decimal` `Field(ge=0)` — the amount withheld. Identity per line: `invoice_amount - sum(deductions) == amount_paid`. Per payment: `total_paid_amount == sum(line_items.amount_paid) - sum(header.deductions)`. `Envelope.payment_index` (0-based) distinguishes payments from one email.
- `ar_pipeline.db.models`: `Email` (statuses `new→classified→extracted→normalized→review→done|error`), `ExtractionSource`, `RawExtraction` (`extraction_source_id`, `payload` jsonb = `{"text": str, "tables": list[list[list[str]]], "meta": dict}`, `extractor_version`), `Extraction` (`email_id`, `canonical` jsonb, `confidence` Numeric(4,3), `is_remittance` bool, `validation_flags` jsonb list, `llm_model` str, `prompt_version` str, `raw_llm_response` jsonb, `status` CHECK `pending_review|approved|rejected`, `reviewed_by`, `reviewed_at`, `created_at`).
- `ar_pipeline.extract.base.ExtractedContent`.
- `ar_pipeline.config.get_settings()` → `Settings` with `llm_provider="anthropic"`, `llm_model="claude-opus-5"`.
- `ar_pipeline.db.base.get_session()`; `ar_pipeline.storage`.
- `ar_pipeline.pipeline.advance`: `advance_once(session, blob_store, vision_extractor, *, batch=20) -> AdvanceStats(classified, extracted, errored)` — advances emails one state per call with **per-source isolation** and **commit per email**. `_step` dispatches on `email.status`. `worker.advance_pipeline()` wired to it.
- `ar_pipeline.extract.vision.VisionExtractor` / `get_vision_extractor` (an existing swappable-LLM precedent to mirror).
- Tests: `tests/fixtures/loader.load_email(name, session, blob_store) -> Email`, `FIXTURE_NAMES` (`01_fwd_bank_advice_pdf` … `06_direct_excel`), `tests/conftest.py` (`db_session`, `_embedded_pg`). `tests/extract/vision_fake.FakeVisionExtractor`.
- Dev tooling: `ruff`, `mypy` (checks `ar_pipeline`, `stub_backend`, `tests`), `.github/workflows/ci.yml`. `live` pytest marker registered; `-m "not live"` in addopts.

## Global Constraints

- Python 3.12; deps via `uv`.
- Sync SQLAlchemy only. No import-time DB connections or network calls in `ar_pipeline/`.
- No real network in tests: the LLM is a fake (`FakeLLMClient`) or a mocked SDK. A real-LLM eval lives behind `@pytest.mark.live` and is deselected by default.
- Every task ends with `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest -q` — all clean.
- Commit message trailer: `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` and nothing else. Verify with `git log -1 --format=%B` after each commit; `git commit --amend` if wrong.
- Monetary values: `Decimal`, never `float`. Amount tolerance for validation checks: `Decimal("0.02")`.
- New code lives under `ar_pipeline/normalize/`. One responsibility per file.
- `mypy`: `anthropic` ships types; if a mock attribute trips mypy in a TEST file, a scoped `# type: ignore[<code>]` with a reason is acceptable there only.

---

### Task 1: `LLMClient` — protocol, Anthropic impl, fake

**Files:**
- Create: `ar_pipeline/normalize/__init__.py`
- Create: `ar_pipeline/normalize/llm_client.py`
- Create: `tests/normalize/__init__.py`
- Create: `tests/normalize/test_llm_client.py`
- Create: `tests/normalize/llm_fake.py`

**Interfaces:**
- Produces:
  - `ar_pipeline.normalize.llm_client.LLMError(Exception)`; `LLMRefused(LLMError)`.
  - `ar_pipeline.normalize.llm_client.LLMClient` — `Protocol`:
    `parse[T: BaseModel](self, *, system: str, user: str, output_model: type[T]) -> T`
    (use a `TypeVar` bound to `pydantic.BaseModel`; return a validated instance of `output_model`).
  - `ar_pipeline.normalize.llm_client.AnthropicLLMClient` — `__init__(self, client: "anthropic.Anthropic | None" = None, model: str = "claude-opus-5")`. Lazy client (build in a `_get_client()` on first use, NOT at import; `import anthropic` under `TYPE_CHECKING` + locally). `parse(...)`: one `client.messages.parse(model=self._model, max_tokens=16000, system=system, messages=[{"role": "user", "content": user}], output_format=output_model)` call. On `response.stop_reason == "refusal"` → raise `LLMRefused`. Return `response.parsed_output`. Wrap `anthropic.APIStatusError` / a `None` `parsed_output` in `LLMError`.
  - `ar_pipeline.normalize.llm_client.get_llm_client() -> LLMClient` — reads `get_settings()`: for `llm_provider == "anthropic"` return `AnthropicLLMClient(model=get_settings().llm_model)`; any other value → raise `LLMError(f"unknown llm_provider {provider!r}")`.
  - `tests/normalize/llm_fake.FakeLLMClient` — `__init__(self, response: BaseModel | None = None, error: Exception | None = None)`. `parse(...)`: records `(system, user, output_model)` in `self.calls`; raises `self._error` if set; else returns `self._response` (must be an instance of the requested `output_model` or the test's problem). A `queue: list[BaseModel]` variant for multi-call tests is fine.

Read the bundled `claude-api` skill — `python/claude-api/tool-use.md` § Structured Outputs — for the exact `client.messages.parse(..., output_format=<PydanticModel>)` shape and `response.parsed_output`.

- [ ] **Step 1: Write the failing test** — `tests/normalize/test_llm_client.py`:
```python
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from ar_pipeline.normalize.llm_client import (
    AnthropicLLMClient,
    LLMError,
    LLMRefused,
    get_llm_client,
)


class _Out(BaseModel):
    value: int


def _resp(parsed, stop="end_turn"):
    r = MagicMock()
    r.stop_reason = stop
    r.parsed_output = parsed
    return r


def test_parse_returns_validated_model():
    client = MagicMock()
    client.messages.parse.return_value = _resp(_Out(value=7))
    out = AnthropicLLMClient(client=client, model="claude-opus-5").parse(
        system="s", user="u", output_model=_Out
    )
    assert out.value == 7
    kwargs = client.messages.parse.call_args.kwargs
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["output_format"] is _Out
    assert kwargs["messages"][0]["content"] == "u"
    assert kwargs["system"] == "s"


def test_parse_raises_on_refusal():
    client = MagicMock()
    client.messages.parse.return_value = _resp(None, stop="refusal")
    with pytest.raises(LLMRefused):
        AnthropicLLMClient(client=client).parse(system="s", user="u", output_model=_Out)


def test_parse_raises_llm_error_on_none_output():
    client = MagicMock()
    client.messages.parse.return_value = _resp(None)
    with pytest.raises(LLMError):
        AnthropicLLMClient(client=client).parse(system="s", user="u", output_model=_Out)


def test_get_llm_client_rejects_unknown_provider(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    import ar_pipeline.config as config_module

    config_module.get_settings.cache_clear()
    with pytest.raises(LLMError):
        get_llm_client()
    config_module.get_settings.cache_clear()
```

- [ ] **Step 2: RED** — `uv run pytest tests/normalize/test_llm_client.py -v` → `ModuleNotFoundError`.
- [ ] **Step 3: Implement** `llm_client.py` + `llm_fake.py` + empty `__init__.py`s. `uv add anthropic` is already a dep.
- [ ] **Step 4: GREEN + gates** (`uv run pytest -q`, ruff, format, mypy).
- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/normalize tests/normalize
git commit -m "feat: swappable LLMClient (Anthropic structured-output impl + fake)"
```

---

### Task 2: Deterministic validators

**Files:**
- Create: `ar_pipeline/normalize/validators.py`
- Create: `tests/normalize/test_validators.py`

**Interfaces:**
- Consumes: `RemittancePayload`, `LineItem`, `Deduction`.
- Produces:
  - `ar_pipeline.normalize.validators.validate_payload(payload: RemittancePayload) -> list[str]` — returns a list of human-readable flag strings (empty = clean). Each check:
    1. **line net identity** — for each line `i`: if `abs(invoice_amount - sum(d.amount for d in deductions) - amount_paid) > 0.02` → `f"line {i} ({invoice_number}): invoice {invoice_amount} - deductions {sum} != amount_paid {amount_paid}"`.
    2. **payment total identity** — `expected = sum(line.amount_paid) - sum(header.deductions)`; if `abs(expected - total_paid_amount) > 0.02` → `f"payment total {total_paid_amount} != sum(line amount_paid) {sum_lines} - header deductions {sum_hdr} = {expected}"`.
    3. **negative amounts** — any `invoice_amount < 0` or `amount_paid < 0` or `total_paid_amount < 0` → a flag naming it (deductions are already `ge=0` by schema).
    4. **duplicate invoice numbers** within the payload → `f"duplicate invoice number: {n}"`.
    5. **empty invoice number** — any `invoice_number.strip() == ""` → flag.
    6. **payment_date sanity** — if `payment_date` is set and it is more than 400 days before any `invoice_date`, or after `date.today() + 2 days` → flag (dates parsed to `date` by the schema already).
    7. **currency** — if `header.currency != "INR"` → `f"non-INR currency: {currency}"` (informational; the pipeline is INR-only for now).
  - `ar_pipeline.normalize.validators.CHECK_VERSION = "1"` (stored in `extraction.prompt_version`? no — see Task 3; this is just a constant to bump if checks change).

- [ ] **Step 1: Write the failing test** — `tests/normalize/test_validators.py`. Build `RemittancePayload`s by hand (helper `_payload(**overrides)`); assert specific flags. Minimum cases:
  - clean payload → `[]`.
  - a line where `invoice_amount - deductions != amount_paid` → exactly one "line 0" flag.
  - `total_paid_amount` off by `1.00` → one "payment total" flag.
  - two line items with the same `invoice_number` → a "duplicate invoice number" flag.
  - `header.currency = "USD"` → a "non-INR" flag; and `abs()` tolerance: off by `0.01` → **no** flag (within tolerance), off by `0.03` → flag.
  - a payment with a header TDS deduction: `total = sum(amount_paid) - tds` reconciles → `[]`.

- [ ] **Step 2: RED. Step 3: implement. Step 4: GREEN + gates.**
- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/normalize/validators.py tests/normalize/test_validators.py
git commit -m "feat: deterministic remittance validators"
```

---

### Task 3: The normalizer

**Files:**
- Create: `ar_pipeline/normalize/normalizer.py`
- Create: `ar_pipeline/normalize/prompt.py`
- Create: `tests/normalize/test_normalizer.py`

**Interfaces:**
- Consumes: `LLMClient`, `RemittancePayload` + parts, `validate_payload`, `RawExtraction` payload dicts.
- Produces:
  - `ar_pipeline.normalize.prompt.PROMPT_VERSION = "1"` (bump on prompt change; stored in `extraction.prompt_version`).
  - `ar_pipeline.normalize.prompt.SYSTEM_PROMPT: str` — the instruction. Must state: role (extract settlement/remittance data), the deduction sign convention + the two identities, "one payload per distinct payment (a payment = one bank transfer; different UTR or value date = different payment)", "do not invent values — null anything not present", "preserve invoice numbers / UTRs verbatim", "currency defaults to INR", "if this is clearly not a remittance advice, set is_remittance=false and payments=[]".
  - `ar_pipeline.normalize.prompt.build_user_message(sender_address: str, subject: str, raw_extractions: list[dict]) -> str` — renders sender/subject then, per raw extraction, its `meta` (kind hint), its `text`, and its `tables` as pipe-delimited rows. Bounded: if the combined text exceeds ~40k chars, keep the first ~40k and append a truncation note (log a warning too — do NOT silently drop).
  - `ar_pipeline.normalize.normalizer.PaymentDraft(BaseModel)` — the LLM's per-payment output (NO envelope): `payer_name: str`, `payer_id: str | None`, `payment_reference: str | None`, `payment_reference_type: str | None`, `payment_date: date | None`, `payment_method: str | None`, `currency: str = "INR"`, `total_paid_amount: Decimal`, `header_deductions: list[Deduction]`, `line_items: list[LineItem]`, `vendor_guess: str | None`, `confidence: float` (0-1). `ConfigDict(extra="forbid")`.
  - `ar_pipeline.normalize.normalizer.NormalizerOutput(BaseModel)` — `is_remittance: bool`, `notes: str = ""`, `payments: list[PaymentDraft] = []`. `ConfigDict(extra="forbid")`.
  - `ar_pipeline.normalize.normalizer.NormalizedPayment` — frozen dataclass: `payload: RemittancePayload`, `confidence: Decimal`, `is_remittance: bool`, `validation_flags: list[str]`, `notes: str`, `raw_llm_response: dict`.
  - `ar_pipeline.normalize.normalizer.normalize_email(*, email_id: str, sender_address: str, subject: str, raw_extractions: list[dict], llm_client: LLMClient, model_name: str) -> list[NormalizedPayment]`:
    1. `user = build_user_message(...)`; `out = llm_client.parse(system=SYSTEM_PROMPT, user=user, output_model=NormalizerOutput)`.
    2. If `not out.is_remittance` OR `out.payments == []`: return a single `NormalizedPayment` with a minimal placeholder `RemittancePayload` **is not possible** (schema requires ≥1 line item + a header). Instead return `[]` and let the caller (Task 4) record a non-remittance `extraction` row with `is_remittance=false`, `canonical={}`, `notes=out.notes`. So `normalize_email` returns `[]` in this case and the caller handles it — OR return a sentinel. **Chosen:** `normalize_email` returns `list[NormalizedPayment]`; an empty list means "LLM says not a remittance / nothing to extract" and the caller writes one `is_remittance=false` extraction row. Put `out.notes` + `out.is_remittance` where the caller can see them: return type stays `list[NormalizedPayment]` but also expose `normalize_email` returning a small result object — simplest: return `tuple[NormalizerOutput, list[NormalizedPayment]]` so the caller has `out.is_remittance` / `out.notes` even when the list is empty.
       **Final interface:** `normalize_email(...) -> tuple[NormalizerOutput, list[NormalizedPayment]]`.
    3. For each `draft` at index `i` in `out.payments`: build `Header(**header fields, deductions=draft.header_deductions)`, `Envelope(extraction_id=str(uuid4()), source_email_id=email_id, payment_index=i, vendor_guess=draft.vendor_guess, extracted_at=datetime.now(UTC), reviewed_by=None)`, `payload = RemittancePayload(envelope=env, header=hdr, line_items=draft.line_items)`. If `RemittancePayload(...)` raises `ValidationError`, append `f"payment {i}: schema validation failed: {exc}"` to flags and skip that payload (still surface it — see Task 4 note).
    4. `flags = validate_payload(payload)`; `confidence = Decimal(str(draft.confidence)).quantize(Decimal("0.001"))` clamped to `[0, 1]`.
    5. `raw_llm_response = out.model_dump(mode="json")`.
    6. Append `NormalizedPayment(payload, confidence, is_remittance=out.is_remittance, validation_flags=flags, notes=out.notes, raw_llm_response=raw_llm_response)`.
  - Errors: `LLMRefused` / `LLMError` from the client propagate (the caller wraps per-email).

- [ ] **Step 1: Write the failing test** — `tests/normalize/test_normalizer.py`, using `FakeLLMClient`:
  - A `FakeLLMClient` returning a `NormalizerOutput` with 1 payment that reconciles → `normalize_email` returns `(out, [one NormalizedPayment])`; the payload's `envelope.payment_index == 0`, `envelope.source_email_id == <email_id>`, `envelope.extraction_id` is a uuid, `validation_flags == []`, `confidence == Decimal("0.900")` (from `0.9`).
  - A `NormalizerOutput` with 2 payments (different UTRs) → 2 `NormalizedPayment`s, `payment_index` 0 and 1.
  - A `NormalizerOutput` with `is_remittance=False`, `payments=[]` → returns `(out, [])`, `out.notes` preserved.
  - A payment whose numbers don't reconcile → the `NormalizedPayment.validation_flags` is non-empty (from `validate_payload`).
  - `build_user_message` — given a fake raw-extraction dict with `text` and `tables`, the output contains the sender, the subject, the text, and a pipe-delimited table row.
  - Truncation: a raw extraction with 60k chars of text → `build_user_message` output is ≤ ~41k and contains a truncation marker.

- [ ] **Step 2: RED. Step 3: implement `prompt.py` + `normalizer.py`. Step 4: GREEN + gates.**
- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/normalize/normalizer.py ar_pipeline/normalize/prompt.py tests/normalize/test_normalizer.py
git commit -m "feat: LLM normalizer (raw extractions -> canonical payloads)"
```

---

### Task 4: `run_normalize` + wire `advance_pipeline`

**Files:**
- Create: `ar_pipeline/normalize/service.py`
- Modify: `ar_pipeline/pipeline/advance.py`
- Modify: `ar_pipeline/worker.py`
- Create: `tests/normalize/test_service.py`
- Modify: `tests/pipeline/test_advance.py`, `tests/pipeline/test_end_to_end.py`, `tests/test_worker.py`

**Interfaces:**
- Produces:
  - `ar_pipeline.normalize.service.normalize_one(session, email, llm_client) -> int` — for one email in `status="extracted"`:
    1. Gather its `RawExtraction` payload dicts: join `RawExtraction` → `ExtractionSource` on `email_id == email.id`, collect `payload` (jsonb dict) for each. Order by source id for determinism.
    2. `out, payments = normalize_email(email_id=str(email.id), sender_address=email.sender_address, subject=email.subject, raw_extractions=[...], llm_client=llm_client, model_name=<settings model>)`.
    3. If `payments`: for each `NormalizedPayment np`, insert `Extraction(email_id=email.id, canonical=np.payload.model_dump(mode="json"), confidence=np.confidence, is_remittance=np.is_remittance, validation_flags=np.validation_flags, llm_model=<model>, prompt_version=PROMPT_VERSION, raw_llm_response=np.raw_llm_response, status="pending_review")`.
    4. Else (empty list — not a remittance / nothing): insert ONE `Extraction(email_id=email.id, canonical={}, confidence=Decimal("0"), is_remittance=out.is_remittance, validation_flags=(["LLM: not a remittance"] if not out.is_remittance else ["LLM returned no payments"]), llm_model=<model>, prompt_version=PROMPT_VERSION, raw_llm_response=out.model_dump(mode="json"), status="pending_review")`.
    5. `email.status = "review"`; `session.flush()`.
    6. Return the number of `Extraction` rows created.
  - `ar_pipeline.normalize.service.get_normalize_llm_client` — thin `= get_llm_client` re-export (so tests patch one name).
- `advance.py`:
  - `advance_once` gains a param: `advance_once(session, blob_store, vision_extractor, llm_client, *, batch=20)`. Add `"extracted"` to `_PENDING_STATUSES`. `_step`: `email.status == "extracted"` → `normalize_one(session, email, llm_client)` then return `"normalized"` (new result string). `advance_once` maps `"normalized"` → a new `AdvanceStats` field `normalized: int`.
  - `AdvanceStats` gains `normalized: int = 0`.
  - Per-email `try/except` + `begin_nested` + `commit()` per email stays exactly as is; an `LLMError`/`LLMRefused` during normalize → that email → `status="error"` + `error_detail`, `errored += 1` (same path as an extractor failure).
- `worker.advance_pipeline()`:
  - Also build `get_llm_client()` (lazy import) and pass it to `advance_once`. Log line gains `%d normalized`.
- `tests/pipeline/test_advance.py` / `test_end_to_end.py`: `advance_once` calls now pass a `FakeLLMClient` (or `FakeVisionExtractor`-style). The e2e test: after the 6 fixtures reach `extracted`, a THIRD `advance_once` call with a `FakeLLMClient` returning a canned per-fixture `NormalizerOutput` → each email → `status == "review"`, ≥1 `Extraction` row with `status=="pending_review"`, `canonical` non-empty for the ones the fake says are remittances. (Keep it simple: the fake can return the same generic 1-payment output for all 6; assert the row exists + `email.status == "review"` + `prompt_version == "1"`.)
- `tests/test_worker.py`: `advance_pipeline()` now also builds an llm client — patch `ar_pipeline.normalize.service.get_llm_client` / the `advance_once` call; keep it a call-through assertion.

- [ ] **Step 1: Write failing tests.** `tests/normalize/test_service.py`:
  - Seed a fixture email, run it through `advance_once` to `extracted` (with `FakeVisionExtractor`), then `normalize_one(db_session, email, FakeLLMClient(response=<1-payment NormalizerOutput>))` → email `status == "review"`, one `Extraction` row `status=="pending_review"`, `canonical["header"]["total_paid_amount"]` present, `llm_model`/`prompt_version` set.
  - `FakeLLMClient(response=<is_remittance=False, payments=[]>)` → one `Extraction` row, `is_remittance is False`, `canonical == {}`, `validation_flags == ["LLM: not a remittance"]`, email → `review`.
  - `FakeLLMClient(response=<2 payments>)` → two `Extraction` rows, `canonical["envelope"]["payment_index"]` 0 and 1.
  - poison: `FakeLLMClient(error=LLMRefused(...))` in a batch of 2 emails via `advance_once` → that email → `status=="error"` + `error_detail`, the other still advances, `errored == 1`.
- [ ] **Step 2: RED. Step 3: implement. Step 4: GREEN + gates** (`uv run pytest -q` — count jumps; ruff/mypy clean).
- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/normalize/service.py ar_pipeline/pipeline/advance.py ar_pipeline/worker.py tests/normalize tests/pipeline tests/test_worker.py
git commit -m "feat: normalize step in advance_pipeline (extracted -> review, N extraction rows/email)"
```

---

### Task 5: Live normalization eval (gated)

**Files:**
- Create: `tests/normalize/test_live_eval.py`
- Create: `tests/normalize/eval_report.py` (helper — pure, unit-testable formatting)

**Interfaces:**
- `tests/normalize/eval_report.summarise(email_name: str, out: NormalizerOutput, flags_per_payment: list[list[str]]) -> str` — a compact human-readable block (payments found, confidence, flags, `is_remittance`). Pure function, unit-tested with a fabricated `NormalizerOutput`.
- `tests/normalize/test_live_eval.py::test_live_normalization_over_fixtures` — `@pytest.mark.live`. Skips unless `ANTHROPIC_API_KEY` is set. For each of `FIXTURE_NAMES`: load the fixture, run it through the real `advance_once` (real `get_vision_extractor` — but every fixture is deterministic-extractable so vision never fires) to `extracted`, then `normalize_one(session, email, get_llm_client())`. Print `eval_report.summarise(...)` for each. **Soft assertions only:** every fixture must yield ≥1 `Extraction` row and `is_remittance is True` for 01–06 (they are all remittances); print (do not assert) the `validation_flags` — the synthetic fixtures are not guaranteed to reconcile to the cent. If a fixture's numbers are wildly off, note it in the report so the fixtures can be tightened later.

- [ ] **Step 1:** write `eval_report.summarise` + its unit test → RED → implement → GREEN.
- [ ] **Step 2:** write the gated live test (it will not run in CI — `-m "not live"`). Verify it is *collected and deselected*: `uv run pytest -q` shows `N deselected` incremented.
- [ ] **Step 3:** gates clean.
- [ ] **Step 4: Commit**
```bash
git add tests/normalize/test_live_eval.py tests/normalize/eval_report.py
git commit -m "test: gated live normalization eval over the fixtures"
```

---

## Self-Review

**1. Spec coverage (`normalize/` section):**
- `llm_client.py` — swappable `LLMClient` interface, Anthropic implementation, structured output → Task 1. ✓
- `normalizer.py` — build a prompt from raw extraction + sender context + schema; LLM → canonical structure + confidence + `is_remittance` + notes; deterministic post-checks → Tasks 2 (validators) + 3 (normalizer). ✓
- `validators.py` — line items sum to header total (within tolerance); dates parseable; single currency → Task 2. ✓
- Writes `extraction` row: `status=pending_review`, canonical JSON, confidence, validation flags, `llm_model`, `prompt_version` → Task 4. ✓
- One email → N payments → N `extraction` rows (`payment_index`) → Tasks 3 + 4. ✓
- LLM timeout/refusal → retry with backoff / mark for review with raw response attached, don't crash the loop → the SDK auto-retries; `LLMRefused`/`LLMError` → the email goes to `error` (per-email isolation, same as extract failures — Task 4). A non-remittance / no-payments result → an `is_remittance=false` `extraction` row, not an error (Task 4 step 4). ✓
- `worker.advance_pipeline()` gains the normalize step → Task 4. ✓
- Deferred by design: per-field confidence (overall per-payment confidence instead — noted); the review UI is Plan 5; delivery is Plan 6; tightening the synthetic fixtures so they reconcile to the cent (Task 5 flags it).

**2. Placeholder scan:** Tasks 2–4 give interfaces + rules + concrete test cases rather than full verbatim bodies for the validators / normalizer / prompt — deliberate (the prompt is prose, the validators are short arithmetic, and the tests pin them). Task 1 and the test skeletons are verbatim. No "TBD"/"handle errors"/"similar to Task N".

**3. Type consistency:** `LLMClient.parse(*, system, user, output_model) -> T` identical across the protocol (Task 1), `AnthropicLLMClient` (Task 1), `FakeLLMClient` (Task 1), and the normalizer's call (Task 3). `NormalizerOutput` / `PaymentDraft` defined Task 3, consumed by Task 4 (`normalize_one`) and Task 5 (`eval_report`). `NormalizedPayment` fields (`payload, confidence, is_remittance, validation_flags, notes, raw_llm_response`) defined Task 3, consumed Task 4. `normalize_email(...) -> tuple[NormalizerOutput, list[NormalizedPayment]]` — Task 3 signature matches Task 4's unpacking. `validate_payload(payload) -> list[str]` — Task 2, called in Task 3. `advance_once(..., llm_client, ...)` + `AdvanceStats.normalized` — Task 4, used in `worker` + all pipeline tests. `PROMPT_VERSION` (Task 3) written to `extraction.prompt_version` (Task 4).

## Execution Handoff

Handled in chat after the plan is saved.
