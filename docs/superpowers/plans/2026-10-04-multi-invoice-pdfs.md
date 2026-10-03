# Multi-Invoice PDFs (Stage 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read a payment advice that settles many invoices (with discount / debit-note adjustment rows, a table spanning pages and wrapped cells) exactly, prove the read against the document's own totals, apply each adjustment to the invoice it reduces, and stop a beneficiary being recorded as the payer.

**Architecture:** The PDF extractor rejoins wrapped cells and page-split tables (`extract/pdf.py`). A new `ar_pipeline/tables/` package finds the one line table in a message, maps its columns once (AI or offline keyword mapper, saved per header layout in `column_mapping`), applies the mapping to every row in code, and asks the AI only for the payment header. Every read is checked against the document's Total row and amount in words (`tables/totals.py`); a table read that disagrees falls back to today's full-AI read. Adjustment lines (`kind: "adjustment"`, `applies_to`) are matched to invoices in code (`ledger/adjustments.py`) and posted to the ledger as `kind = 'adjustment'` rows. One recheck path (`normalize/recheck.py`) recomputes every flag the same way at normalization, on a reviewer's save and after a CSV import.

**Tech Stack:** Python 3.12, FastAPI + Jinja2, SQLAlchemy 2.0 / Postgres (pgserver in tests), Alembic, pdfplumber, reportlab (tests), anthropic SDK, pytest via `uv`.

**Spec:** `docs/superpowers/specs/2026-10-03-threads-and-multi-invoice-pdfs-design.md` — Stage 2 (§1 payment format, §4, §5 stub/UI). The "Planning-time corrections (Stage 2)" section at the end of this plan supersedes the spec where they differ; copy it into the spec in Task 11.

## Open question for the product owner (decided provisionally)

A "…DISCO" row can name an invoice that the **same** advice already pays in full (synthetic fixture: adjustment `2510004583DISCO` ₹12,500 against invoice `CBB2510004583`, whose own row settles gross − TDS). Under decision A (an adjustment reduces that invoice's balance) the ledger then shows that invoice **overpaid by ₹12,500**, and when your books are loaded the check flags "adjustment reduces CBB2510004583, which is already fully paid" — so such advices are never auto-sent. This plan implements A as decided; adjustments are posted as separate `kind = 'adjustment'` ledger rows, so switching to "record as an unapplied customer debit" later touches only `ledger/posting.py` and `ledger/checks.py`.

## Global Constraints

- Payment format: `LineItem.kind` is `"invoice"` (default) or `"adjustment"`; `LineItem.applies_to: str | None`; `Envelope.schema_version` is `"2"`. `DeductionType` gains `"debit_note"`.
- An adjustment line is: `invoice_amount = 0`, exactly the deductions that make it up (`discount` / `debit_note` / `credit_note`), `amount_paid = -sum(deductions)`, `applies_to` = the invoice it reduces or `null`.
- Totals tolerance: ₹1.00. Totals flag text starts with `header: doesn't match the document's own totals`.
- Payer flag text is exactly `header: payer looks like the receiving company — check who paid`.
- Adjustment flags start with `line {i}: adjustment of ` (`{i}` = line index), so the review screen pins them to the line.
- Matching: exact digit match (same payment first, then ledger) → automatic; one inserted or missing digit (never a substituted digit) → suggestion; otherwise a question. Digit strings shorter than 4 never match; near matches need ≥ 6 digits.
- Table path only for a table with ≥ 6 data rows, ≥ 3 columns and ≥ 2 mostly-numeric columns, and only when exactly one such table exists in the message's sources.
- `PROMPT_VERSION` becomes `"5"`; `CHECK_VERSION` becomes `"4"`; `EXTRACTOR_VERSION` becomes `"2"`.
- Money in flags and UI via `ar_pipeline.ledger.money.format_money`.
- No network in tests; `samples/` (real client PII) is never read by tests or committed.
- Line length 100 (ruff). Tests: `uv run pytest <paths> -q`; lint `uv run ruff check ar_pipeline tests`; format `uv run ruff format --check ar_pipeline tests`; types `uv run mypy ar_pipeline`. The full suite takes ~25 s.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `ar_pipeline/schema/canonical.py` (modify) | `kind`, `applies_to`, `schema_version`, `debit_note` |
| `ar_pipeline/normalize/validators.py` (modify) | adjustment-aware checks |
| `ar_pipeline/db/models.py` (modify) | `InvoicePayment.kind`, `Extraction.read_info`, `ColumnMapping` |
| `migrations/versions/0006_multi_invoice.py` (create) | schema for the above |
| `ar_pipeline/tables/__init__.py` (create) | package marker |
| `ar_pipeline/tables/numbers.py` (create) | pure: `parse_amount`, `parse_date` |
| `ar_pipeline/tables/words.py` (create) | pure: `amount_in_words` (Indian) |
| `ar_pipeline/extract/pdf.py` (modify) | wrapped cells, page-split tables, text outside tables |
| `ar_pipeline/tables/models.py` (create) | pure pydantic: `ColumnAssignment`, `MappingOutput`, `HeaderOutput` |
| `ar_pipeline/tables/mapping.py` (create) | pure: `LineTable`, `find_line_table`, `header_signature`, `keyword_mapping`, `validate_mapping`, `apply_mapping` |
| `ar_pipeline/tables/totals.py` (create) | pure: `document_totals`, `totals_flags`, `totals_status` |
| `ar_pipeline/ledger/adjustments.py` (create) | DB: `match_adjustment`, `resolve_adjustments`, `adjustment_flags`, `adjustment_suggestions` |
| `ar_pipeline/ledger/{checks,balance,queries,posting}.py` (modify) | adjustment-aware checks, balances, posting |
| `ar_pipeline/normalize/prompt.py` (modify) | adjustment + payer wording, mapping and header prompts |
| `ar_pipeline/normalize/payer.py` (create) | pure: `payer_flags` |
| `ar_pipeline/normalize/normalizer.py` (modify) | `build_payments` extracted; client names in the prompt |
| `ar_pipeline/normalize/stub_client.py` (modify) | offline mapping + header + client-name skip |
| `ar_pipeline/normalize/recheck.py` (create) | `TRUNCATED_FLAG`, `sticky_flags`, `context_flags`, `recheck`, `refresh_pending` |
| `ar_pipeline/tables/store.py` (create) | DB: `load_mapping`, `save_mapping`, `touch_mapping`, `discard_mapping` |
| `ar_pipeline/tables/reader.py` (create) | `read_by_table` orchestration |
| `ar_pipeline/normalize/service.py` (modify) | table path first, adjustments resolved, `read_info` stored |
| `ar_pipeline/review/{service,app}.py`, templates (modify) | adjustment strip, "Use …", totals badges |
| `stub_backend/app.py` (modify) | adjustment count per payment |
| `tests/tables/advice_pdf.py` (create) | synthetic two-page advice builder (reportlab) |

---

### Task 1: Payment format v2, debit notes, migration 0006

**Files:**
- Modify: `ar_pipeline/schema/canonical.py`, `ar_pipeline/normalize/validators.py`, `ar_pipeline/db/models.py`, `stub_backend/app.py`, `ar_pipeline/review/templates/detail.html`
- Create: `migrations/versions/0006_multi_invoice.py`
- Test: `tests/schema/test_canonical_v2.py`, `tests/normalize/test_validators.py` (append), `tests/stub_backend/test_app.py` (append; create the file if it does not exist)

**Interfaces — Produces:** `LineItem.kind: Literal["invoice","adjustment"] = "invoice"`, `LineItem.applies_to: str | None = None`, `Envelope.schema_version: Literal["2"] = "2"`, `DeductionType` includes `"debit_note"`; `InvoicePayment.kind: str` (`"payment"` default / `"adjustment"`); `Extraction.read_info: dict | None`; model `ColumnMapping` (table `column_mapping`: `id`, `signature` unique, `header` JSONB list, `columns` JSONB role→index, `payer_slug`, `uses`, `created_at`, `last_used_at`).

- [ ] **Step 1: Write the failing tests**

`tests/schema/test_canonical_v2.py`:
```python
from __future__ import annotations

from decimal import Decimal

from ar_pipeline.schema.canonical import Deduction, LineItem, RemittancePayload


def _payload(**line) -> dict:
    return {
        "envelope": {
            "extraction_id": "x",
            "source_email_id": "e",
            "extracted_at": "2026-10-04T00:00:00+00:00",
        },
        "header": {"payer_name": "Acme", "total_paid_amount": "90.00"},
        "line_items": [{"invoice_number": "INV-1", "invoice_amount": "90.00",
                        "amount_paid": "90.00", **line}],
    }


def test_defaults_keep_old_payloads_valid():
    p = RemittancePayload.model_validate(_payload())
    assert p.envelope.schema_version == "2"
    assert p.line_items[0].kind == "invoice"
    assert p.line_items[0].applies_to is None


def test_adjustment_line_round_trips():
    line = LineItem(
        invoice_number="2510004583DISCO",
        invoice_amount=Decimal("0"),
        deductions=[Deduction(type="debit_note", amount=Decimal("12500"))],
        amount_paid=Decimal("-12500"),
        kind="adjustment",
        applies_to="CBB2510004583",
    )
    dumped = line.model_dump(mode="json")
    assert dumped["kind"] == "adjustment" and dumped["applies_to"] == "CBB2510004583"
    assert dumped["deductions"][0]["type"] == "debit_note"
```
Append to `tests/normalize/test_validators.py`:
```python
def test_adjustment_line_may_pay_negative():
    from ar_pipeline.schema.canonical import (
        Deduction, Envelope, Header, LineItem, RemittancePayload,
    )
    from datetime import UTC, datetime
    from decimal import Decimal

    payload = RemittancePayload(
        envelope=Envelope(extraction_id="x", source_email_id="e",
                          extracted_at=datetime(2026, 10, 4, tzinfo=UTC)),
        header=Header(payer_name="Acme", total_paid_amount=Decimal("87500")),
        line_items=[
            LineItem(invoice_number="CBB1", invoice_amount=Decimal("100000"),
                     amount_paid=Decimal("100000")),
            LineItem(invoice_number="1DISCO", invoice_amount=Decimal("0"),
                     deductions=[Deduction(type="discount", amount=Decimal("12500"))],
                     amount_paid=Decimal("-12500"), kind="adjustment"),
        ],
    )
    assert validate_payload(payload) == []


def test_malformed_adjustment_is_flagged():
    from ar_pipeline.schema.canonical import (
        Deduction, Envelope, Header, LineItem, RemittancePayload,
    )
    from datetime import UTC, datetime
    from decimal import Decimal

    payload = RemittancePayload(
        envelope=Envelope(extraction_id="x", source_email_id="e",
                          extracted_at=datetime(2026, 10, 4, tzinfo=UTC)),
        header=Header(payer_name="Acme", total_paid_amount=Decimal("-500")),
        line_items=[
            LineItem(invoice_number="1DISCO", invoice_amount=Decimal("100"),
                     deductions=[Deduction(type="discount", amount=Decimal("600"))],
                     amount_paid=Decimal("-500"), kind="adjustment"),
        ],
    )
    flags = validate_payload(payload)
    assert "line 0: an adjustment must have invoice_amount 0 and amount_paid = -deductions" in flags
```
Append to the stub backend tests (`tests/stub_backend/test_app.py`; reuse that module's client fixture if it has one):
```python
def test_fragment_counts_adjustment_lines():
    from stub_backend.app import _row_html

    html = _row_html({
        "envelope": {"extraction_id": "abc"},
        "header": {"payer_name": "Acme", "total_paid_amount": "1", "currency": "INR"},
        "line_items": [{"kind": "invoice"}, {"kind": "adjustment"}, {"kind": "adjustment"}],
    })
    assert "3 (2 adjustments)" in html
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/schema/test_canonical_v2.py tests/normalize/test_validators.py tests/stub_backend -q`
Expected: FAIL — `schema_version` / `kind` / `debit_note` unknown; no adjustment exemption; no adjustment count.

- [ ] **Step 3: Implement**

`ar_pipeline/schema/canonical.py` — extend the docstring with: "An adjustment line (`kind='adjustment'`: a discount / debit note / credit note against an earlier invoice) has `invoice_amount` 0, its deductions, `amount_paid = -sum(deductions)`, and `applies_to` = the invoice it reduces." Then:
```python
DeductionType = Literal[
    "tds", "credit_note", "debit_note", "advance_adjustment", "discount", "rounding", "other"
]
LineKind = Literal["invoice", "adjustment"]
```
`Envelope` gains (first field after `model_config`): `schema_version: Literal["2"] = "2"`.
`LineItem` gains (after `amount_paid`): `kind: LineKind = "invoice"` and `applies_to: str | None = None`.

`ar_pipeline/normalize/validators.py`: `CHECK_VERSION = "4"`. In check 3 replace the `amount_paid < 0` test with `if line.amount_paid < 0 and line.kind != "adjustment":`. Add check 10 before `return flags`:
```python
    # 10. adjustment shape: nothing invoiced, pays back exactly its deductions
    for i, line in enumerate(line_items):
        if line.kind != "adjustment":
            continue
        sum_ded = sum((d.amount for d in line.deductions), Decimal("0"))
        if line.invoice_amount != 0 or abs(line.amount_paid + sum_ded) > _TOLERANCE:
            flags.append(
                f"line {i}: an adjustment must have invoice_amount 0 and amount_paid = -deductions"
            )
```

`ar_pipeline/db/models.py`:
- `INVOICE_PAYMENT_KINDS = ("payment", "adjustment")` next to the other status tuples.
- `InvoicePayment.kind: Mapped[str] = mapped_column(String(12), default="payment", server_default="payment")` and add `CheckConstraint(_in("kind", INVOICE_PAYMENT_KINDS), name="ck_invoice_payment_kind")` to its `__table_args__`.
- `Extraction.read_info: Mapped[dict | None] = mapped_column(MutableDict.as_mutable(JSONB))` (after `duplicate_of_id`).
- New model at the end:
```python
class ColumnMapping(Base):
    """A learned column layout for one payment-advice table header (spec §4.2):
    role -> column index, reused for every later advice with the same header."""

    __tablename__ = "column_mapping"

    id: Mapped[uuid.UUID] = _uuid_pk()
    signature: Mapped[str] = mapped_column(String(64))
    header: Mapped[list] = mapped_column(JSONB)
    columns: Mapped[dict] = mapped_column(JSONB)
    payer_slug: Mapped[str | None] = mapped_column(Text)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("signature", name="uq_column_mapping_signature"),)
```

`migrations/versions/0006_multi_invoice.py`:
```python
"""multi-invoice PDFs: adjustment ledger rows, saved column mappings, read info"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoice_payment",
        sa.Column("kind", sa.String(length=12), server_default="payment", nullable=False),
    )
    op.create_check_constraint(
        "ck_invoice_payment_kind", "invoice_payment", "kind IN ('payment', 'adjustment')"
    )
    op.add_column(
        "extraction",
        sa.Column("read_info", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_table(
        "column_mapping",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column("header", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("columns", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payer_slug", sa.Text(), nullable=True),
        sa.Column("uses", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("signature", name="uq_column_mapping_signature"),
    )


def downgrade() -> None:
    op.drop_table("column_mapping")
    op.drop_column("extraction", "read_info")
    op.drop_constraint("ck_invoice_payment_kind", "invoice_payment", type_="check")
    op.drop_column("invoice_payment", "kind")
```
Match the exact constraint text and column forms the existing migrations use if `tests/db/test_migrations.py::test_no_model_migration_drift` reports a difference.

`stub_backend/app.py` `_row_html`: replace the line-count cell value with
```python
    adjustments = sum(1 for li in line_items if li.get("kind") == "adjustment")
    count = f"{len(line_items)} ({adjustments} adjustments)" if adjustments else str(len(line_items))
    line_item_count = html.escape(count)
```

`ar_pipeline/review/templates/detail.html`: add `"debit_note"` after `"credit_note"` in the deduction-type list of the `deduction_row` macro, and add a `debit_note` entry to the `deductionGlossary` dialog worded like its neighbours: "debit_note — the payer's debit note against an earlier invoice (e.g. a price or quantity claim); appears on adjustment lines."

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/schema tests/normalize/test_validators.py tests/stub_backend tests/db -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/schema ar_pipeline/normalize/validators.py ar_pipeline/db/models.py migrations/versions/0006_multi_invoice.py stub_backend/app.py ar_pipeline/review/templates/detail.html tests
git commit -m "feat(pdfs): payment format v2 with adjustment lines and debit notes"
```

---

### Task 2: Amounts, dates and amounts in words

**Files:**
- Create: `ar_pipeline/tables/__init__.py`, `ar_pipeline/tables/numbers.py`, `ar_pipeline/tables/words.py`
- Test: `tests/tables/__init__.py`, `tests/tables/test_numbers.py`, `tests/tables/test_words.py`

**Interfaces — Produces:** `parse_amount(raw: str | None) -> Decimal | None`, `parse_date(raw: str | None) -> date | None`, `amount_in_words(text: str) -> Decimal | None`.

- [ ] **Step 1: Write the failing tests**

`tests/tables/test_numbers.py`:
```python
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ar_pipeline.tables.numbers import parse_amount, parse_date


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2,804,859.17", Decimal("2804859.17")),
        ("1,23,456.78", Decimal("123456.78")),
        ("2,377.00-", Decimal("-2377.00")),
        ("-15,924.00", Decimal("-15924.00")),
        ("(1,234.50)", Decimal("-1234.50")),
        ("Rs. 500", Decimal("500")),
        ("₹ 1,000.00", Decimal("1000.00")),
        ("0.00", Decimal("0.00")),
        ("", None),
        (None, None),
        ("27.11.2025", None),
        ("2510004583DISCO", None),
    ],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("27.11.2025", date(2025, 11, 27)),
        ("05/11/2025", date(2025, 11, 5)),
        ("05-11-25", date(2025, 11, 5)),
        ("2025-11-05", date(2025, 11, 5)),
        ("5-Nov-2025", date(2025, 11, 5)),
        ("", None),
        ("not a date", None),
    ],
)
def test_parse_date_day_first(raw, expected):
    assert parse_date(raw) == expected
```
`tests/tables/test_words.py`:
```python
from __future__ import annotations

from decimal import Decimal

import pytest

from ar_pipeline.tables.words import amount_in_words


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "for rupees ( ONE CRORE THIRTY ONE LAKH THIRTEEN THOUSAND THREE HUNDRED TWENTY "
            "FIVE RupeesTHIRTY NINE Paise ) as detailed below.",
            Decimal("13113325.39"),
        ),
        ("Rupees One Lakh Only", Decimal("100000.00")),
        ("Rs. Five Thousand and Fifty Paise only", Decimal("5000.50")),
        ("INR: Rupees Twelve Lacs Fifty Thousand only", Decimal("1250000.00")),
        ("Rupees Nine Hundred Ninety Nine only", Decimal("999.00")),
        ("one invoice and two credit notes", None),
        ("no amount here at all", None),
    ],
)
def test_amount_in_words(text, expected):
    assert amount_in_words(text) == expected
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/tables -q`
Expected: FAIL — modules do not exist.

- [ ] **Step 3: Implement**

`ar_pipeline/tables/__init__.py`: `"""Reading payment-advice line tables (spec §4)."""`
`tests/tables/__init__.py`: empty.

`ar_pipeline/tables/numbers.py`:
```python
"""Amounts and dates as payment advices print them. Pure — no DB, no I/O."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_CURRENCY_RE = re.compile(r"(?i)rs\.?|inr|₹")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_DATE_FORMATS = (
    "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%y", "%d/%m/%y", "%d-%m-%y",
    "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%d-%b-%y", "%d %B %Y",
)


def parse_amount(raw: str | None) -> Decimal | None:
    """'2,377.00-' -> -2377.00, '(1,234.50)' -> -1234.50, '1,23,456.78' -> 123456.78;
    None for blanks and anything that is not a plain amount (dates, codes)."""
    if raw is None:
        return None
    s = _CURRENCY_RE.sub("", str(raw)).strip().replace(" ", "")
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s.endswith("-"):
        negative, s = True, s[:-1]
    if s.startswith("-"):
        negative, s = True, s[1:]
    s = s.replace(",", "")
    if not _NUMBER_RE.fullmatch(s):
        return None
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return -value if negative else value


def parse_date(raw: str | None) -> date | None:
    """Day-first, as Indian advices print dates."""
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None
```

`ar_pipeline/tables/words.py`:
```python
"""The amount in words an Indian payment advice prints ("ONE CRORE THIRTY ONE
LAKH … Rupees THIRTY NINE Paise") as a Decimal. Pure — no DB, no I/O."""

from __future__ import annotations

import re
from decimal import Decimal

_UNITS = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen "
        "fourteen fifteen sixteen seventeen eighteen nineteen".split()
    )
}
_TENS = {
    w: 10 * (i + 2)
    for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())
}
_SCALES = {
    "thousand": 10**3, "lakh": 10**5, "lakhs": 10**5, "lac": 10**5, "lacs": 10**5,
    "crore": 10**7, "crores": 10**7,
}
_RUPEE = {"rupee", "rupees"}
_PAISE = {"paise", "paisa"}
_FILLER = {"and", "only"}
_VOCAB = set(_UNITS) | set(_TENS) | {"hundred"} | set(_SCALES) | _RUPEE | _PAISE | _FILLER
# "RupeesTHIRTY" is printed glued; split the currency words off first
_SPLIT_RE = re.compile(r"(?i)(rupees?|paise|paisa)")
_WORD_RE = re.compile(r"[a-z]+")


def _value(words: list[str]) -> int | None:
    total = current = 0
    seen = False
    for w in words:
        if w in _UNITS:
            current += _UNITS[w]
        elif w in _TENS:
            current += _TENS[w]
        elif w == "hundred":
            current = (current or 1) * 100
        elif w in _SCALES:
            total += (current or 1) * _SCALES[w]
            current = 0
        elif w in _FILLER:
            continue
        else:
            return None
        seen = True
    return total + current if seen else None


def _runs(text: str) -> list[list[str]]:
    tokens = _WORD_RE.findall(_SPLIT_RE.sub(r" \1 ", text).lower())
    runs: list[list[str]] = []
    current: list[str] = []
    for t in tokens:
        if t in _VOCAB:
            current.append(t)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _parse_run(run: list[str]) -> Decimal | None:
    while run and run[0] in _RUPEE | _FILLER:
        run = run[1:]
    while run and run[-1] in _FILLER:
        run = run[:-1]
    paise = 0
    if run and run[-1] in _PAISE:
        run = run[:-1]
        if any(w in _RUPEE for w in run):
            cut = max(i for i, w in enumerate(run) if w in _RUPEE)
        elif "and" in run:
            cut = len(run) - 1 - run[::-1].index("and")
        else:
            cut = -1
        rupee_words, paise_words = (run[:cut], run[cut + 1 :]) if cut >= 0 else ([], run)
        p = _value(paise_words)
        if p is None or p > 99:
            return None
        paise = p
    else:
        rupee_words = [w for w in run if w not in _RUPEE]
    rupees = _value(rupee_words) if rupee_words else 0
    if rupees is None:
        return None
    return (Decimal(rupees) + Decimal(paise) / 100).quantize(Decimal("0.01"))


def amount_in_words(text: str) -> Decimal | None:
    """The longest run of number words that names rupees or paise, or None."""
    best: tuple[int, Decimal] | None = None
    for run in _runs(text):
        if not set(run) & (_RUPEE | _PAISE):
            continue
        number_words = sum(1 for w in run if w not in _RUPEE | _PAISE | _FILLER)
        if number_words < 2:
            continue
        amount = _parse_run(run)
        if amount is not None and (best is None or number_words > best[0]):
            best = (number_words, amount)
    return best[1] if best else None
```
Note: "Rupees One Lakh Only" has two number words (`one`, `lakh`). "one invoice and two credit notes" has no rupee/paise word → None.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/tables -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/tables tests/tables
git commit -m "feat(pdfs): parse advice amounts, dates and amounts in words"
```

---

### Task 3: PDF reading — wrapped cells, page-split tables, text outside tables

**Files:**
- Modify: `ar_pipeline/extract/pdf.py`, `ar_pipeline/extract/base.py` (`EXTRACTOR_VERSION = "2"`)
- Create: `tests/tables/advice_pdf.py`
- Test: `tests/extract/test_pdf.py` (append)

**Interfaces — Produces:** `extract_pdf(data) -> ExtractedContent` whose `tables` are cleaned and merged across pages and whose `text` excludes characters inside table boxes; `meta = {"page_count": int, "table_count": int}`. Test helper `tests.tables.advice_pdf`: `build_advice_pdf(*, beneficiary: str = "ACME METALS LTD") -> bytes`, constants `HEADER`, `NET` (Decimal), `WORDS`.

- [ ] **Step 1: Write the synthetic advice builder and failing tests**

`tests/tables/advice_pdf.py`:
```python
"""A synthetic multi-invoice payment advice in the style of a real one: a ruled
table over two pages, wrapped bill numbers, "…DISCO" adjustment rows,
trailing-minus TDS, a one-word continuation row at the top of page 2, a Total
row and the amount in words. Every name and number here is invented."""

from __future__ import annotations

import io
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HEADER = ["BillNo", "BillDate", "A/C RefNo", "GrossAmount", "Adv/Debit", "TDS", "Net Payment"]
NET = Decimal("6809764.78")
WORDS = "SIXTY EIGHT LAKH NINE THOUSAND SEVEN HUNDRED SIXTY FOUR RupeesSEVENTY EIGHT Paise"


def _adj(number: str, day: str, ref: str, amount: str) -> list[str]:
    return [number, day, ref, "", amount, "0.00", f"-{amount}"]


PAGE1 = [
    ["CBB2510004\n583", "27.11.2025", "5105847648", "1,250,000.00", "", "1,250.00-",
     "1,248,750.00"],
    _adj("2510004583DIS\nCO", "27.11.2025", "1700003207", "12,500.00"),
    _adj("2510004516DIS\nCO", "24.11.2025", "1700003206", "300.00"),
    _adj("2510004515DIS\nCO", "24.11.2025", "1700003205", "11,200.00"),
    _adj("2510004431DIS\nCO", "19.11.2025", "1700003200", "7,450.00"),
    ["CBB2510026\n174", "24.09.2025", "5105849306", "3,400,500.50", "", "3,400.50-",
     "3,397,100.00"],
    ["CBB2510003\n5016", "28.11.2025", "5105847817", "875,320.40", "", "875.32-", "874,445.08"],
    ["CBB2510003\n5015", "28.11.2025", "5105847816", "410,000.00", "", "410.00-", "409,590.00"],
    ["CBB2510003\n5014", "28.11.2025", "5105847815", "1,020,300.00", "", "1,020.30-",
     "1,019,279.70"],
    _adj("2510035017DIS\nCO", "28.11.2025", "1700003193", "9,800.00"),
    _adj("2510035016DIS", "28.11.2025", "1700003192", "4,100.00"),
]
PAGE2 = [
    ["CO", "", "", "", "", "", ""],
    _adj("2510035015DIS\nCO", "28.11.2025", "1700003191", "2,250.00"),
    _adj("2510035014DIS\nCO", "28.11.2025", "1700003190", "5,600.00"),
    _adj("2510031885DIS\nCO", "05.11.2025", "1700003189", "3,300.00"),
    _adj("2510031884DIS\nCO", "05.11.2025", "1700003188", "10,150.00"),
    _adj("2510031883DIS\nCO", "05.11.2025", "1700003187", "29,900.00"),
    _adj("2510031882DIS\nCO", "05.11.2025", "1700003186", "3,350.00"),
    _adj("2510031881DIS\nCO", "05.11.2025", "1700003185", "13,700.00"),
    _adj("2510031879DIS\nCO", "05.11.2025", "1700003184", "17,000.00"),
    _adj("2510031878DIS\nCO", "05.11.2025", "1700003183", "8,800.00"),
    ["Total", "", "", "6,956,120.90", "139,400.00", "6,956.12-", "6,809,764.78"],
]


def build_advice_pdf(*, beneficiary: str = "ACME METALS LTD") -> bytes:
    body = getSampleStyleSheet()["Normal"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
    )
    grid = TableStyle(
        [("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("FONTSIZE", (0, 0), (-1, -1), 7)]
    )
    widths = [28 * mm, 20 * mm, 22 * mm, 28 * mm, 22 * mm, 22 * mm, 28 * mm]

    def table(rows: list[list[str]]) -> Table:
        t = Table([HEADER, *rows], colWidths=widths)
        t.setStyle(grid)
        return t

    doc.build([
        Paragraph("CONTINENTAL BUS BODY BUILDERS LIMITED", body),
        Paragraph("PAYMENT ADVICE", body),
        Paragraph(
            f"Vendor Code : 220417 Vendor Name : {beneficiary} Document No : 1500009001", body
        ),
        Paragraph("Document Date: 17.01.2026", body),
        Paragraph(
            f"We have made payment through Net Banking for rupees ( {WORDS} ) as detailed below.",
            body,
        ),
        Spacer(1, 4 * mm),
        table(PAGE1),
        PageBreak(),
        table(PAGE2),
        Spacer(1, 4 * mm),
        Paragraph("RTGS/NEFT Reference : RTGS PAYMENT", body),
    ])
    return buf.getvalue()
```
Append to `tests/extract/test_pdf.py`:
```python
def test_advice_table_is_cleaned_and_merged_across_pages() -> None:
    from tests.tables.advice_pdf import HEADER, build_advice_pdf

    raw = extract_pdf(build_advice_pdf())

    assert raw.meta["page_count"] == 2
    assert len(raw.tables) == 1
    table = raw.tables[0]
    assert table[0] == ["BillNo", "BillDate", "A/C RefNo", "GrossAmount", "Adv/Debit", "TDS",
                        "Net Payment"] == HEADER
    numbers = [row[0] for row in table[1:]]
    assert numbers[0] == "CBB2510004583"             # wrapped cell glued, no space
    assert numbers[6] == "CBB25100035016"
    assert numbers[10] == "2510035016DISCO"           # continuation row glued across pages
    assert "CO" not in numbers
    assert table[-1][0] == "Total"
    assert len(table) == 1 + 20 + 1                   # header + 20 lines + total
    # text outside the table only: the words line is there, table rows are not
    from ar_pipeline.tables.words import amount_in_words
    from tests.tables.advice_pdf import NET

    assert amount_in_words(raw.text) == NET          # Task 2 is done before this task
    assert "SIXTY EIGHT" in raw.text
    assert "Document No : 1500009001" in raw.text
    assert "RTGS/NEFT Reference : RTGS PAYMENT" in raw.text
    assert "1,248,750.00" not in raw.text
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/extract/test_pdf.py -q`
Expected: FAIL — two tables, unglued cells, table rows in the text.

- [ ] **Step 3: Implement**

`ar_pipeline/extract/base.py`: `EXTRACTOR_VERSION = "2"`.

`ar_pipeline/extract/pdf.py`:
```python
"""PDF extractor -- page text outside ruled tables, plus the tables themselves,
cleaned the way payment advices need (spec §4.1): wrapped cells rejoined,
a table continued on the next page under the same header merged into one, and
a one-word continuation row at the top of a page glued back onto the row it
belongs to."""

from __future__ import annotations

from io import BytesIO

import pdfplumber

from ar_pipeline.extract.base import ExtractedContent

Table = list[list[str]]


def _glue(a: str, b: str) -> str:
    """'WBBEL2510004' + '583' -> 'WBBEL2510004583'; fragments with spaces keep one."""
    if not a:
        return b
    if not b:
        return a
    return f"{a} {b}" if " " in a or " " in b else a + b


def _cell(raw: object) -> str:
    out = ""
    for part in str(raw or "").split("\n"):
        out = _glue(out, part.strip())
    return out


def _is_continuation(row: list[str]) -> bool:
    filled = [c for c in row if c]
    return bool(filled) and len(filled) * 3 <= len(row) and all(
        " " not in c and len(c) <= 12 for c in filled
    )


def _merge(tables: list[Table]) -> list[Table]:
    merged: list[Table] = []
    for table in tables:
        if merged and table and merged[-1] and table[0] == merged[-1][0]:
            rest = table[1:]
            if rest and _is_continuation(rest[0]) and len(merged[-1]) > 1:
                prev = merged[-1][-1]
                for i, cell in enumerate(rest[0]):
                    if cell and i < len(prev):
                        prev[i] = _glue(prev[i], cell)
                rest = rest[1:]
            merged[-1].extend(rest)
        else:
            merged.append(table)
    return merged


def extract_pdf(data: bytes) -> ExtractedContent:
    page_texts: list[str] = []
    tables: list[Table] = []

    with pdfplumber.open(BytesIO(data)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            found = page.find_tables()
            outside = page
            for table in found:
                outside = outside.outside_bbox(table.bbox, strict=False)
            page_texts.append(outside.extract_text() or "")
            for table in found:
                rows = [[_cell(c) for c in row] for row in table.extract()]
                rows = [row for row in rows if any(row)]
                if rows:
                    tables.append(rows)

    merged = _merge(tables)
    return ExtractedContent(
        text="\n\n".join(page_texts),
        tables=merged,
        meta={"page_count": page_count, "table_count": len(merged)},
    )
```
If pdfplumber renders the builder's cells differently from the assertions (e.g. the header cell text), adjust **only** `tests/tables/advice_pdf.py` layout values (column widths, font size) until the real extractor output matches — never special-case the extractor for the fixture. Existing `test_extract_pdf_*` tests use unruled PDFs and must still pass unchanged.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/extract tests/pipeline -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/extract tests/tables/advice_pdf.py tests/extract/test_pdf.py
git commit -m "feat(pdfs): rejoin wrapped cells and page-split tables in PDF advices"
```

---

### Task 4: Line-table detection and column mapping

**Files:**
- Create: `ar_pipeline/tables/models.py`, `ar_pipeline/tables/mapping.py`
- Test: `tests/tables/test_mapping.py`

**Interfaces — Consumes:** `parse_amount`, `parse_date` (Task 2); `LineItem`, `Deduction` with `kind` (Task 1).
**Produces:**
- `models.py`: `Role` (Literal of `ROLES`), `ROLES: tuple[str, ...]`, `ColumnAssignment(index: int, role: Role)`, `MappingOutput(is_line_table: bool, columns: list[ColumnAssignment] = [], notes: str = "")`, `HeaderOutput(is_remittance: bool, notes: str = "", payer_name: str = "", payer_id, payment_reference, payment_reference_type, payment_date: date | None, payment_method, currency: str = "INR", vendor_guess, confidence: float = 0.0)`.
- `mapping.py`: `MIN_DATA_ROWS = 6`; `ColumnMap = dict[str, int]`; `LineTable(source_index, table_index, header, rows, total_row)`; `MappedTable(lines: list[LineItem], column_totals: dict[str, Decimal])`; `MappingError(ValueError)`; `find_line_table(raws: list[dict]) -> LineTable | None`; `header_signature(header: list[str]) -> str`; `keyword_mapping(header: list[str]) -> ColumnMap | None`; `validate_mapping(out: MappingOutput, header: list[str]) -> ColumnMap | None`; `mapping_output(cols: ColumnMap | None) -> MappingOutput`; `apply_mapping(table: LineTable, cols: ColumnMap) -> MappedTable`.

- [ ] **Step 1: Write the failing tests**

`tests/tables/test_mapping.py`:
```python
from __future__ import annotations

from decimal import Decimal

from ar_pipeline.extract.pdf import extract_pdf
from ar_pipeline.tables.mapping import (
    apply_mapping,
    find_line_table,
    header_signature,
    keyword_mapping,
    validate_mapping,
)
from ar_pipeline.tables.models import ColumnAssignment, MappingOutput
from tests.tables.advice_pdf import HEADER, build_advice_pdf

EXPECTED = {
    "invoice_number": 0, "invoice_date": 1, "invoice_amount": 3,
    "adjustment": 4, "tds": 5, "amount_paid": 6,
}


def _raws():
    return [extract_pdf(build_advice_pdf()).to_payload()]


def test_finds_the_one_line_table_and_its_total_row():
    table = find_line_table(_raws())
    assert table is not None
    assert table.header == HEADER
    assert len(table.rows) == 20
    assert table.total_row is not None and table.total_row[0] == "Total"


def test_small_or_duplicate_tables_are_not_line_tables():
    small = {"text": "", "tables": [[["Invoice", "Amount", "Net"], ["A1", "1.00", "1.00"]]]}
    assert find_line_table([small]) is None
    raws = _raws()
    assert find_line_table(raws + raws) is None  # two candidates: not "one clear table"


def test_keyword_mapping_reads_glued_headers():
    assert keyword_mapping(HEADER) == EXPECTED
    assert keyword_mapping(["Bill No", "Bill Date", "A/C RefNo", "Gross Amount", "TDS",
                            "Net Payment"]) == {
        "invoice_number": 0, "invoice_date": 1, "invoice_amount": 3, "tds": 4, "amount_paid": 5,
    }
    assert keyword_mapping(["Name", "City"]) is None


def test_signature_ignores_case_spacing_and_punctuation():
    assert header_signature(HEADER) == header_signature(
        ["Bill No", "Bill date", "A/C Ref No", "Gross Amount", "Adv / Debit", "tds", "NetPayment"]
    )


def test_validate_mapping_rejects_bad_ai_output():
    good = MappingOutput(is_line_table=True, columns=[
        ColumnAssignment(index=i, role=r) for r, i in EXPECTED.items()
    ])
    assert validate_mapping(good, HEADER) == EXPECTED
    assert validate_mapping(MappingOutput(is_line_table=False), HEADER) is None
    twice = MappingOutput(is_line_table=True, columns=[
        ColumnAssignment(index=0, role="invoice_number"),
        ColumnAssignment(index=1, role="invoice_number"),
        ColumnAssignment(index=6, role="amount_paid"),
    ])
    assert validate_mapping(twice, HEADER) is None
    out_of_range = MappingOutput(is_line_table=True, columns=[
        ColumnAssignment(index=0, role="invoice_number"),
        ColumnAssignment(index=9, role="amount_paid"),
    ])
    assert validate_mapping(out_of_range, HEADER) is None


def test_apply_mapping_copies_every_row_exactly():
    table = find_line_table(_raws())
    mapped = apply_mapping(table, EXPECTED)
    lines = mapped.lines
    assert len(lines) == 20
    first = lines[0]
    assert first.kind == "invoice" and first.invoice_number == "CBB2510004583"
    assert first.invoice_amount == Decimal("1250000.00")
    assert [(d.type, d.amount) for d in first.deductions] == [("tds", Decimal("1250.00"))]
    assert first.amount_paid == Decimal("1248750.00")
    adj = lines[1]
    assert adj.kind == "adjustment" and adj.invoice_number == "2510004583DISCO"
    assert adj.invoice_amount == 0 and adj.amount_paid == Decimal("-12500.00")
    assert [(d.type, d.amount) for d in adj.deductions] == [("discount", Decimal("12500.00"))]
    assert sum(1 for li in lines if li.kind == "adjustment") == 15
    assert sum(li.amount_paid for li in lines) == Decimal("6809764.78")
    assert mapped.column_totals == {
        "invoice_amount": Decimal("6956120.90"), "adjustment": Decimal("139400.00"),
        "tds": Decimal("6956.12"), "amount_paid": Decimal("6809764.78"),
    }


def test_negative_gross_rows_are_adjustments_too():
    header = ["Bill No", "Bill Date", "Gross Amount", "TDS", "Net Payment"]
    rows = [["CBB1", "01.01.2026", "1,000.00", "1.00", "999.00"]] * 5 + [
        ["1DISCO", "01.01.2026", "-15,924.00", "0.00", "-15,924.00"]
    ]
    table = find_line_table([{"text": "", "tables": [[header, *rows]]}])
    mapped = apply_mapping(table, keyword_mapping(header))
    assert mapped.lines[-1].kind == "adjustment"
    assert mapped.lines[-1].amount_paid == Decimal("-15924.00")


def test_rows_with_different_bank_references_are_not_one_payment():
    import pytest

    from ar_pipeline.tables.mapping import MappingError

    header = ["Invoice No", "UTR", "Amount", "Net Paid"]
    rows = [[f"INV{i}", f"UTR{i}", "10.00", "10.00"] for i in range(6)]
    table = find_line_table([{"text": "", "tables": [[header, *rows]]}])
    with pytest.raises(MappingError):
        apply_mapping(table, keyword_mapping(header))


def test_two_thousand_rows():
    header = ["Bill No", "Gross Amount", "TDS", "Net Payment"]
    rows = [[f"CBB{i:06d}", "1,000.00", "1.00-", "999.00"] for i in range(2000)]
    table = find_line_table([{"text": "", "tables": [[header, *rows]]}])
    mapped = apply_mapping(table, keyword_mapping(header))
    assert len(mapped.lines) == 2000
    assert sum(li.amount_paid for li in mapped.lines) == Decimal("1998000.00")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/tables/test_mapping.py -q`
Expected: FAIL — modules do not exist.

- [ ] **Step 3: Implement**

`ar_pipeline/tables/models.py`:
```python
"""Structured AI outputs for the table path (spec §4.2). Pure pydantic."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROLES = (
    "invoice_number", "invoice_date", "invoice_amount", "tds", "adjustment",
    "other_deduction", "amount_paid", "payment_reference", "ignore",
)
Role = Literal[
    "invoice_number", "invoice_date", "invoice_amount", "tds", "adjustment",
    "other_deduction", "amount_paid", "payment_reference", "ignore",
]


class ColumnAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    role: Role


class MappingOutput(BaseModel):
    """Which line-item field each column of the table holds."""

    model_config = ConfigDict(extra="forbid")

    is_line_table: bool
    columns: list[ColumnAssignment] = Field(default_factory=list)
    notes: str = ""


class HeaderOutput(BaseModel):
    """The payment header, read while the table's rows are read in code."""

    model_config = ConfigDict(extra="forbid")

    is_remittance: bool
    notes: str = ""
    payer_name: str = ""
    payer_id: str | None = None
    payment_reference: str | None = None
    payment_reference_type: str | None = None
    payment_date: date | None = None
    payment_method: str | None = None
    currency: str = "INR"
    vendor_guess: str | None = None
    confidence: float = 0.0
```

`ar_pipeline/tables/mapping.py`:
```python
"""Find the one line table in a message's sources, map its columns to
line-item fields, and apply the mapping to every row in code — figures copied
exactly, any number of rows (spec §4.2). Pure — no DB, no network."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from ar_pipeline.schema.canonical import Deduction, LineItem
from ar_pipeline.tables.models import ColumnAssignment, MappingOutput
from ar_pipeline.tables.numbers import parse_amount, parse_date

MIN_DATA_ROWS = 6
_REQUIRED = {"invoice_number", "amount_paid"}
_TOTAL_RE = re.compile(r"^\s*(?:grand\s*)?total\b|^\s*net\s*payable", re.I)
_ZERO = Decimal("0")

ColumnMap = dict[str, int]


class MappingError(ValueError):
    """The mapping does not fit this table; use the full-AI read."""


@dataclass(frozen=True)
class LineTable:
    source_index: int
    table_index: int
    header: list[str]
    rows: list[list[str]]
    total_row: list[str] | None


@dataclass(frozen=True)
class MappedTable:
    lines: list[LineItem]
    column_totals: dict[str, Decimal]


def _compact(cell: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(cell).lower())


def header_signature(header: list[str]) -> str:
    return hashlib.sha256("|".join(_compact(c) for c in header).encode()).hexdigest()


def _numeric_share(rows: list[list[str]], col: int) -> float:
    hits = sum(1 for r in rows if col < len(r) and parse_amount(r[col]) is not None)
    return hits / len(rows) if rows else 0.0


def find_line_table(raws: list[dict]) -> LineTable | None:
    found: list[LineTable] = []
    for si, raw in enumerate(raws):
        for ti, table in enumerate(raw.get("tables") or []):
            rows = [[str(c) for c in r] for r in table if any(str(c).strip() for c in r)]
            if len(rows) < 2 or len(rows[0]) < 3:
                continue
            header, body = rows[0], rows[1:]
            total = None
            if any(_TOTAL_RE.match(c) for c in body[-1] if c):
                total, body = body[-1], body[:-1]
            if len(body) < MIN_DATA_ROWS:
                continue
            numeric = sum(1 for i in range(len(header)) if _numeric_share(body, i) >= 0.5)
            if numeric < 2:
                continue
            found.append(LineTable(si, ti, header, body, total))
    return found[0] if len(found) == 1 else None


_RULES: list[tuple[str, Callable[[str], bool]]] = [
    ("tds", lambda c: "tds" in c),
    ("invoice_date", lambda c: "date" in c),
    ("payment_reference", lambda c: any(w in c for w in ("utr", "rtgs", "neft"))),
    ("invoice_number", lambda c: c in {"invoice", "bill", "billno", "billnumber", "invoiceno",
                                       "invoicenumber", "invno", "docno", "documentno"}),
    ("invoice_amount", lambda c: "gross" in c or c in {"invoiceamount", "billamount",
                                                         "invoicevalue"}),
    ("adjustment", lambda c: c.startswith("adv") or any(w in c for w in ("debit", "adjust",
                                                                         "discount"))),
    ("amount_paid", lambda c: c.startswith("net") or c in {"amountpaid", "paidamount",
                                                            "payment", "paymentamount",
                                                            "amount"}),
]


def keyword_mapping(header: list[str]) -> ColumnMap | None:
    """The offline mapper: each column gets the first matching role, each role
    its first column. None when invoice number or amount paid is missing."""
    cols: ColumnMap = {}
    for i, cell in enumerate(header):
        c = _compact(cell)
        if not c:
            continue
        for role, rule in _RULES:
            if role not in cols and rule(c):
                cols[role] = i
                break
    return cols if _REQUIRED <= cols.keys() else None


def mapping_output(cols: ColumnMap | None) -> MappingOutput:
    if cols is None:
        return MappingOutput(is_line_table=False)
    return MappingOutput(
        is_line_table=True,
        columns=[ColumnAssignment(index=i, role=r) for r, i in cols.items()],  # type: ignore[arg-type]
    )


def validate_mapping(out: MappingOutput, header: list[str]) -> ColumnMap | None:
    if not out.is_line_table:
        return None
    cols: ColumnMap = {}
    for a in out.columns:
        if a.role == "ignore":
            continue
        if not 0 <= a.index < len(header) or a.role in cols or a.index in cols.values():
            return None
        cols[a.role] = a.index
    return cols if _REQUIRED <= cols.keys() else None


def _get(row: list[str], cols: ColumnMap, role: str) -> str:
    i = cols.get(role)
    return row[i].strip() if i is not None and i < len(row) else ""


def _abs(raw: str) -> Decimal:
    value = parse_amount(raw)
    return abs(value) if value is not None else _ZERO


def apply_mapping(table: LineTable, cols: ColumnMap) -> MappedTable:
    if "payment_reference" in cols:
        refs = {_get(r, cols, "payment_reference") for r in table.rows} - {""}
        if len(refs) > 1:
            raise MappingError("rows carry different bank references — several payments")
    lines: list[LineItem] = []
    for row in table.rows:
        number = _get(row, cols, "invoice_number")
        gross = parse_amount(_get(row, cols, "invoice_amount"))
        paid = parse_amount(_get(row, cols, "amount_paid"))
        tds = _abs(_get(row, cols, "tds"))
        adj = _abs(_get(row, cols, "adjustment"))
        other = _abs(_get(row, cols, "other_deduction"))
        if gross is None and paid is None and adj == 0:
            continue  # a heading or note row
        day = parse_date(_get(row, cols, "invoice_date"))
        if (paid is not None and paid < 0) or ((gross is None or gross == 0) and adj > 0):
            amount = adj if adj > 0 else abs(paid if paid is not None else gross or _ZERO)
            if amount == 0:
                continue
            kind = "discount" if "DISC" in number.upper() else "debit_note"
            lines.append(LineItem(
                invoice_number=number, invoice_date=day, invoice_amount=_ZERO,
                deductions=[Deduction(type=kind, amount=amount)],
                amount_paid=-amount, kind="adjustment",
            ))
            continue
        deductions = [
            Deduction(type=t, amount=a)  # type: ignore[arg-type]
            for t, a in (("tds", tds), ("advance_adjustment", adj), ("other", other))
            if a > 0
        ]
        total_ded = sum((d.amount for d in deductions), _ZERO)
        invoice_amount = gross if gross is not None else (paid or _ZERO) + total_ded
        amount_paid = paid if paid is not None else invoice_amount - total_ded
        lines.append(LineItem(
            invoice_number=number, invoice_date=day, invoice_amount=invoice_amount,
            deductions=deductions, amount_paid=amount_paid,
        ))
    if not lines:
        raise MappingError("no line rows")
    totals: dict[str, Decimal] = {}
    if table.total_row is not None:
        for role in ("invoice_amount", "tds", "adjustment", "amount_paid"):
            value = parse_amount(_get(table.total_row, cols, role))
            if value is not None:
                totals[role] = abs(value) if role in ("tds", "adjustment") else value
    return MappedTable(lines, totals)
```
Note: `_get` reads the Total row by the mapped columns, so the `"Total"` label in column 0 is ignored. The `# type: ignore` comments are only needed if mypy rejects the plain `str` → `Literal` assignments; remove any that mypy reports as unused.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/tables -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/tables tests/tables/test_mapping.py
git commit -m "feat(pdfs): find the line table, map its columns, apply the mapping in code"
```

---

### Task 5: Totals check

**Files:**
- Create: `ar_pipeline/tables/totals.py`
- Test: `tests/tables/test_totals.py`

**Interfaces — Consumes:** `amount_in_words` (Task 2), `RemittancePayload` with `kind` (Task 1).
**Produces:** `TOTALS_TOLERANCE = Decimal("1.00")`; `FLAG_TOTALS = "header: doesn't match the document's own totals"`; `document_totals(column_totals: dict[str, Decimal], texts: list[str]) -> dict[str, str]` (keys among `invoice_amount`, `tds`, `adjustment`, `amount_paid`, `words`); `totals_flags(payload, document: dict[str, str] | None) -> list[str]`; `totals_status(payload, document) -> Literal["match","mismatch","not_found"]`.

- [ ] **Step 1: Write the failing tests**

`tests/tables/test_totals.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from ar_pipeline.extract.pdf import extract_pdf
from ar_pipeline.schema.canonical import Envelope, Header, RemittancePayload
from ar_pipeline.tables.mapping import apply_mapping, find_line_table, keyword_mapping
from ar_pipeline.tables.totals import (
    FLAG_TOTALS,
    document_totals,
    totals_flags,
    totals_status,
)
from tests.tables.advice_pdf import build_advice_pdf


def _read():
    raw = extract_pdf(build_advice_pdf()).to_payload()
    table = find_line_table([raw])
    mapped = apply_mapping(table, keyword_mapping(table.header))
    payload = RemittancePayload(
        envelope=Envelope(extraction_id="x", source_email_id="e",
                          extracted_at=datetime(2026, 10, 4, tzinfo=UTC)),
        header=Header(payer_name="Continental",
                      total_paid_amount=sum(li.amount_paid for li in mapped.lines)),
        line_items=mapped.lines,
    )
    return payload, document_totals(mapped.column_totals, [raw["text"]])


def test_document_totals_include_amount_in_words():
    _payload, doc = _read()
    assert doc == {
        "invoice_amount": "6956120.90", "adjustment": "139400.00", "tds": "6956.12",
        "amount_paid": "6809764.78", "words": "6809764.78",
    }


def test_matching_read_has_no_flags():
    payload, doc = _read()
    assert totals_flags(payload, doc) == []
    assert totals_status(payload, doc) == "match"


def test_a_dropped_row_is_caught():
    payload, doc = _read()
    short = payload.model_copy(update={"line_items": payload.line_items[:-1]})
    flags = totals_flags(short, doc)
    assert flags and all(f.startswith(FLAG_TOTALS) for f in flags)
    assert any("adjustments: document ₹1,39,400.00, extracted ₹1,30,600.00" in f for f in flags)
    assert totals_status(short, doc) == "mismatch"


def test_within_one_rupee_is_a_match_and_no_totals_is_not_found():
    payload, doc = _read()
    nudged = payload.model_copy(update={"header": payload.header.model_copy(
        update={"total_paid_amount": payload.header.total_paid_amount + Decimal("0.75")})})
    assert totals_flags(nudged, {"words": doc["words"]}) == []
    assert totals_flags(payload, {}) == [] and totals_status(payload, None) == "not_found"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/tables/test_totals.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

`ar_pipeline/tables/totals.py`:
```python
"""The document's own totals against what was extracted (spec §4.4): the
Total row's columns and the amount in words. Pure — no DB, no network."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from ar_pipeline.ledger.money import format_money
from ar_pipeline.schema.canonical import RemittancePayload
from ar_pipeline.tables.words import amount_in_words

TOTALS_TOLERANCE = Decimal("1.00")
FLAG_TOTALS = "header: doesn't match the document's own totals"
_LABELS = {
    "invoice_amount": "gross",
    "tds": "TDS",
    "adjustment": "adjustments",
    "amount_paid": "net paid",
    "words": "amount in words",
}
_ZERO = Decimal("0")


def document_totals(column_totals: dict[str, Decimal], texts: list[str]) -> dict[str, str]:
    out = {k: str(v) for k, v in column_totals.items()}
    words = amount_in_words("\n".join(texts))
    if words is not None:
        out["words"] = str(words)
    return out


def _extracted(payload: RemittancePayload) -> dict[str, Decimal]:
    invoices = [li for li in payload.line_items if li.kind == "invoice"]
    adjustments = [li for li in payload.line_items if li.kind == "adjustment"]

    def deducted(lines: list, types: set[str]) -> Decimal:
        return sum((d.amount for li in lines for d in li.deductions if d.type in types), _ZERO)

    return {
        "invoice_amount": sum((li.invoice_amount for li in invoices), _ZERO),
        "tds": deducted(invoices, {"tds"}),
        "adjustment": sum((d.amount for li in adjustments for d in li.deductions), _ZERO)
        + deducted(invoices, {"advance_adjustment"}),
        "amount_paid": sum((li.amount_paid for li in payload.line_items), _ZERO),
        "words": payload.header.total_paid_amount,
    }


def totals_flags(payload: RemittancePayload, document: dict[str, str] | None) -> list[str]:
    if not document:
        return []
    got = _extracted(payload)
    currency = payload.header.currency
    flags: list[str] = []
    for key, printed in document.items():
        if key not in got:
            continue
        doc = Decimal(printed)
        if abs(doc - got[key]) > TOTALS_TOLERANCE:
            flags.append(
                f"{FLAG_TOTALS} ({_LABELS[key]}: document {format_money(doc, currency)}, "
                f"extracted {format_money(got[key], currency)})"
            )
    return flags


def totals_status(
    payload: RemittancePayload, document: dict[str, str] | None
) -> Literal["match", "mismatch", "not_found"]:
    if not document:
        return "not_found"
    return "mismatch" if totals_flags(payload, document) else "match"
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/tables -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/tables/totals.py tests/tables/test_totals.py
git commit -m "feat(pdfs): check every read against the document's own totals"
```

---

### Task 6: Adjustments in the ledger

**Files:**
- Create: `ar_pipeline/ledger/adjustments.py`
- Modify: `ar_pipeline/ledger/checks.py`, `ar_pipeline/ledger/balance.py`, `ar_pipeline/ledger/queries.py`, `ar_pipeline/ledger/posting.py`, `ar_pipeline/review/templates/invoice_detail.html`
- Test: `tests/ledger/test_adjustments.py`

**Interfaces — Consumes:** `LineItem.kind/applies_to` and `InvoicePayment.kind` (Task 1).
**Produces:**
- `adjustments.py`: `AdjustmentMatch(target: str | None, suggestions: list[str])`; `match_adjustment(number: str, same: list[str], ledger: list[str]) -> AdjustmentMatch`; `resolve_adjustments(session, payload) -> RemittancePayload`; `adjustment_flags(session, payload) -> list[str]`; `adjustment_suggestions(session, payload, index: int) -> list[str]`.
- `balance.line_target(line: dict) -> tuple[str, Decimal]` — `(invoice number the line settles, amount)`; an adjustment returns `(applies_to or "", sum of its deductions)`.
- `LineLedger` gains `kind: str = "invoice"` and `adjustment_suggestions: list[str] = field(default_factory=list)`; `LedgerEntry` gains `kind: str = "payment"`.
- `post_extraction` posts invoice lines first, then each adjustment with a target found in the ledger as `InvoicePayment(kind="adjustment", amount_paid=0, deductions_total=X, settled=X)`.

- [ ] **Step 1: Write the failing tests**

`tests/ledger/test_adjustments.py`:
```python
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from ar_pipeline.db.models import Invoice, InvoicePayment
from ar_pipeline.ledger.adjustments import (
    adjustment_flags,
    adjustment_suggestions,
    match_adjustment,
    resolve_adjustments,
)
from ar_pipeline.ledger.balance import line_target
from ar_pipeline.ledger.checks import check_against_ledger
from ar_pipeline.ledger.matching import number_key
from ar_pipeline.ledger.posting import post_extraction
from ar_pipeline.schema.canonical import RemittancePayload
from tests.ledger.conftest import canonical_for


def _adjustment(number: str, amount: str, applies_to: str | None = None) -> dict:
    return {
        "invoice_number": number, "invoice_date": None, "invoice_amount": "0",
        "deductions": [{"type": "discount", "amount": amount, "reason": None}],
        "amount_paid": f"-{amount}", "kind": "adjustment", "applies_to": applies_to,
    }


def _payload(*adjustments: dict) -> dict:
    c = canonical_for(invoice_number="CBB2510004583", invoice_amount="1000.00",
                      amount_paid="1000.00")
    c["line_items"] += list(adjustments)
    paid = Decimal("1000.00") - sum(Decimal(a["deductions"][0]["amount"]) for a in adjustments)
    c["header"]["total_paid_amount"] = str(paid)
    return c


def test_match_rules():
    same = ["CBB2510004583", "CBB25100035016"]
    ledger = ["CBB2510004516", "CBB2510004515"]
    assert match_adjustment("2510004583DISCO", same, ledger).target == "CBB2510004583"
    assert match_adjustment("2510004516DISCO", same, ledger).target == "CBB2510004516"
    near = match_adjustment("2510035016DISCO", same, ledger)
    assert near.target is None and near.suggestions == ["CBB25100035016"]
    # a substituted digit is a neighbouring invoice, never a suggestion
    assert match_adjustment("2510004517DISCO", same, ledger) == match_adjustment(
        "9999999999DISCO", same, ledger
    )
    assert match_adjustment("2510004517DISCO", same, ledger).suggestions == []
    assert match_adjustment("12DISCO", same, ledger).suggestions == []


def test_resolve_and_flags(db_session, make_invoice):
    make_invoice("CBB2510004516", amount="300.00")
    payload = RemittancePayload.model_validate(_payload(
        _adjustment("2510004583DISCO", "50.00"),
        _adjustment("2510004516DISCO", "20.00"),
        _adjustment("7777777777DISCO", "10.00"),
    ))
    resolved = resolve_adjustments(db_session, payload)
    assert [li.applies_to for li in resolved.line_items] == [
        None, "CBB2510004583", "CBB2510004516", None,
    ]
    flags = adjustment_flags(db_session, resolved)
    assert flags == ["line 3: adjustment of ₹10.00 — which invoice does it reduce?"]


def test_suggestion_flag_and_list(db_session):
    payload = RemittancePayload.model_validate(_payload(_adjustment("251000458DISCO", "5.00")))
    assert adjustment_flags(db_session, payload) == [
        "line 1: adjustment of ₹5.00 — did you mean CBB2510004583?"
    ]
    assert adjustment_suggestions(db_session, payload, 1) == ["CBB2510004583"]


def test_ledger_checks_skip_adjustment_numbers(db_session, make_invoice):
    make_invoice("CBB2510004583", amount="1000.00")
    payload = RemittancePayload.model_validate(
        _payload(_adjustment("2510004583DISCO", "50.00", applies_to="CBB2510004583"))
    )
    flags = check_against_ledger(db_session, payload)
    assert not any("2510004583DISCO" in f for f in flags)
    # the invoice is settled in full by line 0, so the adjustment over-settles it
    assert "line 1: adjustment reduces CBB2510004583, which is already fully paid" in flags


def test_line_target():
    assert line_target(_adjustment("1DISCO", "5.00", "INV-9")) == ("INV-9", Decimal("5.00"))
    assert line_target({"invoice_number": "INV-1", "amount_paid": "90", "deductions": [
        {"amount": "10"}]}) == ("INV-1", Decimal("100"))


def test_posting_applies_adjustments_to_their_invoice(db_session, seed_extraction):
    ext = seed_extraction(_payload(
        _adjustment("2510004583DISCO", "50.00", applies_to="CBB2510004583"),
        _adjustment("7777777777DISCO", "10.00"),
    ))
    assert post_extraction(db_session, ext) == 2
    invoice = db_session.scalar(
        select(Invoice).where(Invoice.number_key == number_key("CBB2510004583"))
    )
    rows = db_session.scalars(
        select(InvoicePayment).where(InvoicePayment.invoice_id == invoice.id)
        .order_by(InvoicePayment.line_index)
    ).all()
    assert [(r.kind, r.settled) for r in rows] == [
        ("payment", Decimal("1000.00")), ("adjustment", Decimal("50.00")),
    ]
    assert db_session.scalar(
        select(Invoice).where(Invoice.number_key == number_key("2510004583DISCO"))
    ) is None
```
`make_invoice` exists in `tests/ledger/conftest.py`; check its signature and adapt the keyword names (e.g. `amount=`) to it. If no `seed_extraction` fixture exists there, add one to `tests/ledger/conftest.py`:
```python
@pytest.fixture
def seed_extraction(db_session):
    """An email + an approved remittance extraction holding `canonical`."""

    def _make(canonical: dict) -> Extraction:
        email = Email(
            internet_message_id=f"m-{uuid.uuid4()}", sender_address="a@b.com",
            sender_domain="b.com", subject="advice",
            received_at=datetime(2026, 10, 1, tzinfo=UTC), status="done",
        )
        db_session.add(email)
        db_session.flush()
        ext = Extraction(email_id=email.id, canonical=canonical, is_remittance=True,
                         status="approved")
        db_session.add(ext)
        db_session.flush()
        return ext

    return _make
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/ledger/test_adjustments.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

`ar_pipeline/ledger/adjustments.py`:
```python
"""Which invoice an adjustment line (a discount / debit note against an earlier
invoice) reduces — spec §4.3. Exact digit match (this payment first, then the
ledger) is automatic; one inserted or missing digit is only a suggestion; a
substituted digit is a neighbouring invoice and never suggested."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Invoice
from ar_pipeline.ledger.matching import number_key
from ar_pipeline.ledger.money import format_money
from ar_pipeline.schema.canonical import RemittancePayload

_MIN_DIGITS = 4
_MIN_NEAR = 6
_LIMIT = 3


@dataclass(frozen=True)
class AdjustmentMatch:
    target: str | None
    suggestions: list[str]


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def _one_digit_apart(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) != 1:
        return False
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1 :] == short for i in range(len(long_)))


def _unique(numbers: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for n in numbers:
        k = number_key(n)
        if k and k not in seen:
            seen.add(k)
            out.append(n)
    return out


def match_adjustment(number: str, same: list[str], ledger: list[str]) -> AdjustmentMatch:
    d = _digits(number)
    if len(d) < _MIN_DIGITS:
        return AdjustmentMatch(None, [])
    for pool in (same, ledger):
        exact = _unique([n for n in pool if _digits(n) == d])
        if len(exact) == 1:
            return AdjustmentMatch(exact[0], [])
        if exact:
            return AdjustmentMatch(None, exact[:_LIMIT])
    if len(d) < _MIN_NEAR:
        return AdjustmentMatch(None, [])
    near = _unique([n for n in same + ledger if _one_digit_apart(d, _digits(n))])
    return AdjustmentMatch(None, near[:_LIMIT])


def _pools(session: Session, payload: RemittancePayload) -> tuple[list[str], list[str]]:
    same = [li.invoice_number for li in payload.line_items
            if li.kind == "invoice" and number_key(li.invoice_number)]
    ledger = list(session.scalars(select(Invoice.invoice_number)))
    return same, ledger


def resolve_adjustments(session: Session, payload: RemittancePayload) -> RemittancePayload:
    """Fill ``applies_to`` where exactly one invoice matches by digits."""
    same, ledger = _pools(session, payload)
    lines = []
    for line in payload.line_items:
        if line.kind == "adjustment" and not line.applies_to:
            found = match_adjustment(line.invoice_number, same, ledger)
            if found.target:
                line = line.model_copy(update={"applies_to": found.target})
        lines.append(line)
    return payload.model_copy(update={"line_items": lines})


def adjustment_suggestions(session: Session, payload: RemittancePayload, index: int) -> list[str]:
    line = payload.line_items[index]
    if line.kind != "adjustment":
        return []
    same, ledger = _pools(session, payload)
    return match_adjustment(line.invoice_number, same, ledger).suggestions


def adjustment_flags(session: Session, payload: RemittancePayload) -> list[str]:
    same, ledger = _pools(session, payload)
    known = {number_key(n) for n in same + ledger}
    currency = payload.header.currency
    flags: list[str] = []
    for i, line in enumerate(payload.line_items):
        if line.kind != "adjustment":
            continue
        money = format_money(sum(d.amount for d in line.deductions), currency)
        if line.applies_to:
            if number_key(line.applies_to) not in known:
                flags.append(
                    f"line {i}: adjustment of {money} reduces {line.applies_to}, "
                    "which isn't in this payment or your invoices"
                )
            continue
        found = match_adjustment(line.invoice_number, same, ledger)
        if found.suggestions:
            flags.append(
                f"line {i}: adjustment of {money} — did you mean {', '.join(found.suggestions)}?"
            )
        else:
            flags.append(f"line {i}: adjustment of {money} — which invoice does it reduce?")
    return flags
```

`ar_pipeline/ledger/balance.py` — add after `line_settled`:
```python
def line_target(line: dict) -> tuple[str, Decimal]:
    """(invoice number the line settles, amount it settles). An adjustment
    settles its deductions against the invoice it reduces (``applies_to``)."""
    if line.get("kind") == "adjustment":
        deductions = line.get("deductions") or []
        return (
            str(line.get("applies_to") or ""),
            sum((_dec(d.get("amount")) for d in deductions), _ZERO),
        )
    return str(line.get("invoice_number") or ""), line_settled(line)
```
and in `awaiting_by_key` replace the inner loop body with:
```python
            number, settled = line_target(line)
            key = number_key(number)
            if key:
                out[key] += settled
```

`ar_pipeline/ledger/checks.py` `check_against_ledger`:
- first statement inside the line loop: `if line.kind == "adjustment": continue`
- after the loop, before `return flags`:
```python
    for i, line in enumerate(payload.line_items):
        if line.kind != "adjustment" or not number_key(line.applies_to or ""):
            continue
        key = number_key(line.applies_to or "")
        target = session.scalar(select(Invoice).where(Invoice.number_key == key))
        if target is None or target.currency != header.currency:
            continue
        amount = sum((d.amount for d in line.deductions), Decimal("0"))
        bal = balance_for(session, target)
        before = earlier.get(key, Decimal("0"))
        earlier[key] = before + amount
        if status_for(target.amount, bal.paid + before) in ("paid", "overpaid"):
            flags.append(
                f"line {i}: adjustment reduces {target.invoice_number}, which is already fully paid"
            )
    flags.extend(adjustment_flags(session, payload))
```
(import `adjustment_flags` from `ar_pipeline.ledger.adjustments`.)

`ar_pipeline/ledger/posting.py` `post_extraction`: iterate `ordered = [(i, li) for i, li in enumerate(payload.line_items) if li.kind == "invoice"] + [(i, li) for i, li in enumerate(payload.line_items) if li.kind == "adjustment"]`. Keep the existing per-line `already` check. For an adjustment line:
```python
        if line.kind == "adjustment":
            amount = sum((d.amount for d in line.deductions), Decimal("0"))
            target = find_invoice(session, line.applies_to or "")
            if target is None or amount == 0:
                # never invent an invoice from an adjustment
                log.info("extraction %s line %s: adjustment has no ledger invoice", extraction.id, i)
                continue
            session.add(InvoicePayment(
                invoice_id=target.id, extraction_id=extraction.id, line_index=i, kind="adjustment",
                amount_paid=Decimal("0"), deductions_total=amount, settled=amount,
                currency=header.currency, payment_reference=header.payment_reference,
                payment_date=header.payment_date,
            ))
            posted += 1
            continue
```
(Invoice lines keep the existing code; they now pass `kind="payment"` implicitly by default. Add a flush after the invoice-line pass so a same-payment invoice created there is found by `find_invoice`.)

`ar_pipeline/ledger/queries.py`:
- `LedgerEntry` gains `kind: str = "payment"` (last field); `invoice_detail` passes `pay.kind`.
- `invoice_detail` awaiting loop: `number, settled = line_target(line)`; compare `number_key(number) == inv.number_key`; append `settled`.
- `LineLedger` gains `kind: str = "invoice"` and `adjustment_suggestions: list[str] = field(default_factory=list)` (import `field`).
- `line_ledgers`: build `payload = RemittancePayload.model_validate(canonical)` inside `try/except ValidationError: payload = None` once. Loop with `enumerate`. For each line: `kind = str(line.get("kind") or "invoice")`; `number, settled = line_target(line)`; for an adjustment with no `number` append `LineLedger("", currency, None, None, None, None, None, None, _ZERO, [], kind="adjustment", adjustment_suggestions=adjustment_suggestions(session, payload, i) if payload else [])` and continue; adjustments never get `near_matches` suggestions; use `settled` (not `line_settled(line)`) for `after_this`; pass `kind=kind` on every `LineLedger`.

`ar_pipeline/review/templates/invoice_detail.html` — in the reference cell: `{{ e.reference or "—" }}{% if e.kind == "adjustment" %} <span class="badge">adjustment</span>{% endif %}`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ledger tests/review -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/ledger ar_pipeline/review/templates/invoice_detail.html tests/ledger
git commit -m "feat(pdfs): match adjustments to invoices and post them to the ledger"
```

---

### Task 7: Prompts, payer guard, normalizer refactor, offline stub

**Files:**
- Modify: `ar_pipeline/normalize/prompt.py`, `ar_pipeline/normalize/normalizer.py`, `ar_pipeline/normalize/stub_client.py`
- Create: `ar_pipeline/normalize/payer.py`
- Test: `tests/normalize/test_payer.py`, `tests/normalize/test_prompt_v5.py`, `tests/normalize/test_stub_client.py` (append)

**Interfaces — Consumes:** `MappingOutput`, `HeaderOutput` (Task 4); `keyword_mapping`, `mapping_output`, `LineTable` (Task 4).
**Produces:**
- `prompt.py`: `PROMPT_VERSION = "5"`; `MAPPING_SYSTEM_PROMPT`; `HEADER_SYSTEM_PROMPT`; `system_prompt_for(client_names: list[str], base: str = SYSTEM_PROMPT) -> str`; `build_mapping_message(table: LineTable) -> str`; `build_header_message(sender, subject, raws, table: LineTable) -> str`.
- `normalizer.py`: `build_payments(email_id: str, out: NormalizerOutput) -> list[NormalizedPayment]`; `normalize_email(..., client_names: list[str] | None = None)`.
- `payer.py`: `FLAG_PAYER_IS_CLIENT`; `payer_flags(payload, client_names: list[str]) -> list[str]`.
- Stub: `parse(output_model=MappingOutput)` → keyword mapping of the `Columns:` block; `parse(output_model=HeaderOutput)` → header guess; payer guess skips names in `CLIENT_NAMES`.

- [ ] **Step 1: Write the failing tests**

`tests/normalize/test_payer.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from ar_pipeline.normalize.payer import FLAG_PAYER_IS_CLIENT, payer_flags
from ar_pipeline.schema.canonical import Envelope, Header, LineItem, RemittancePayload


def _payload(payer: str) -> RemittancePayload:
    return RemittancePayload(
        envelope=Envelope(extraction_id="x", source_email_id="e",
                          extracted_at=datetime(2026, 10, 4, tzinfo=UTC)),
        header=Header(payer_name=payer, total_paid_amount=Decimal("1")),
        line_items=[LineItem(invoice_number="A", invoice_amount=Decimal("1"),
                             amount_paid=Decimal("1"))],
    )


def test_payer_named_like_the_client_is_flagged():
    assert payer_flags(_payload("ACME METALS LTD"), ["Acme Metals"]) == [FLAG_PAYER_IS_CLIENT]
    assert payer_flags(_payload("Continental Bus Body Builders"), ["Acme Metals"]) == []
    assert payer_flags(_payload("ACME METALS LTD"), []) == []
    assert payer_flags(_payload(""), ["Acme Metals"]) == []
```
`tests/normalize/test_prompt_v5.py`:
```python
from __future__ import annotations

from ar_pipeline.normalize.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_header_message,
    build_mapping_message,
    system_prompt_for,
)
from ar_pipeline.tables.mapping import find_line_table
from tests.tables.advice_pdf import build_advice_pdf


def _raws():
    from ar_pipeline.extract.pdf import extract_pdf

    return [extract_pdf(build_advice_pdf()).to_payload()]


def test_version_and_adjustment_wording():
    assert PROMPT_VERSION == "5"
    assert 'kind="adjustment"' in SYSTEM_PROMPT and "applies_to" in SYSTEM_PROMPT


def test_client_names_are_appended_only_when_set():
    assert system_prompt_for([]) == SYSTEM_PROMPT
    prompt = system_prompt_for(["Acme Metals", "Acme Metals Ltd"])
    assert prompt.startswith(SYSTEM_PROMPT)
    assert "Acme Metals, Acme Metals Ltd" in prompt and "never the payer" in prompt


def test_mapping_message_shows_columns_and_three_rows():
    table = find_line_table(_raws())
    msg = build_mapping_message(table)
    assert "0 = BillNo" in msg and "6 = Net Payment" in msg
    assert msg.count("\n2510004516DISCO") == 1 and "2510004515DISCO" not in msg


def test_header_message_elides_the_table():
    raws = _raws()
    table = find_line_table(raws)
    msg = build_header_message("ap@ourco.com", "FW: advice", raws, table)
    assert "CBB2510004583" in msg and "[... 17 more rows read separately ...]" in msg
    assert "2510031878DISCO" not in msg
    assert "Total | " in msg
```
Append to `tests/normalize/test_stub_client.py`:
```python
def test_stub_maps_columns_and_reads_header(monkeypatch):
    from ar_pipeline.config import get_settings
    from ar_pipeline.extract.pdf import extract_pdf
    from ar_pipeline.normalize.prompt import build_header_message, build_mapping_message
    from ar_pipeline.normalize.stub_client import StubLLMClient
    from ar_pipeline.tables.mapping import find_line_table, validate_mapping
    from ar_pipeline.tables.models import HeaderOutput, MappingOutput
    from tests.tables.advice_pdf import build_advice_pdf

    monkeypatch.setenv("CLIENT_NAMES", "Acme Metals")
    get_settings.cache_clear()
    raws = [extract_pdf(build_advice_pdf()).to_payload()]
    table = find_line_table(raws)
    stub = StubLLMClient()
    mapping = stub.parse(system="", user=build_mapping_message(table), output_model=MappingOutput)
    assert validate_mapping(mapping, table.header)["amount_paid"] == 6
    header = stub.parse(system="", user=build_header_message("a@b.c", "s", raws, table),
                        output_model=HeaderOutput)
    assert header.is_remittance
    assert header.payer_name == "CONTINENTAL BUS BODY BUILDERS LIMITED"
    get_settings.cache_clear()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/normalize/test_payer.py tests/normalize/test_prompt_v5.py tests/normalize/test_stub_client.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`ar_pipeline/normalize/payer.py`:
```python
"""Payer vs beneficiary guard (spec §4.5). Pure."""

from __future__ import annotations

from ar_pipeline.ledger.matching import payer_slug, payers_differ
from ar_pipeline.schema.canonical import RemittancePayload

FLAG_PAYER_IS_CLIENT = "header: payer looks like the receiving company — check who paid"


def payer_flags(payload: RemittancePayload, client_names: list[str]) -> list[str]:
    payer = payload.header.payer_name
    if not payer_slug(payer):
        return []
    for name in client_names:
        if payer_slug(name) and not payers_differ(payer, name):
            return [FLAG_PAYER_IS_CLIENT]
    return []
```

`ar_pipeline/normalize/prompt.py`:
- `PROMPT_VERSION = "5"`.
- In `SYSTEM_PROMPT`, insert this paragraph after the "Deductions." paragraph:
```
Adjustments. Some advices list rows that are not invoices but reduce an \
earlier invoice — discounts, debit notes, credit notes, often numbered after \
the invoice they reduce (e.g. "2510004583DISCO"). Emit each as a line item with \
kind="adjustment": invoice_number = the adjustment's own number, \
invoice_amount = 0, one deduction {type: discount | debit_note | credit_note, \
amount: X}, amount_paid = -X, and applies_to = the invoice it reduces when the \
advice names it, else null. Every ordinary invoice line has kind="invoice".
```
- Add below `SYSTEM_PROMPT`:
```python
MAPPING_SYSTEM_PROMPT = """\
You map the columns of one payment-advice table to line-item fields. The table \
lists the invoices that one payment settles, possibly with adjustment rows \
(discounts, debit notes). Give each column index one role: invoice_number (the \
row's bill / invoice / document number), invoice_date, invoice_amount (gross \
invoice value), tds (tax deducted at source), adjustment (advance / debit / \
discount column), other_deduction, amount_paid (net amount paid for the row), \
payment_reference (a bank UTR / RTGS / NEFT number per row), or ignore. Use each \
role at most once; invoice_number and amount_paid are required. If the table is \
not a list of invoices settled by one payment — for example every row is a \
separate bank transfer, or it is not a payment table — set is_line_table=false.
"""

HEADER_SYSTEM_PROMPT = """\
You read the header of one payment advice: who paid, when, how, and the \
payment's reference. Its invoice table has already been read separately; only \
its first rows are shown. is_remittance is false if this is not a payment \
advice or remittance at all. payer_name is the company that made the payment — \
usually the letterhead, not the "Vendor Name" or beneficiary, which is the \
company receiving the money. payer_id is the payer's vendor / customer code if \
shown. payment_reference is the bank UTR / RTGS / NEFT reference with \
payment_reference_type "utr" / "rtgs" / "neft"; when there is no bank \
reference but the payer's own document number is shown (e.g. "Document No : \
1500005408"), put that number with type "payer_document"; otherwise null. \
payment_date is the payment or document date. currency is a three-letter ISO \
code (INR unless stated). confidence (0 to 1) is how sure you are of these \
fields; notes is anything a reviewer should know. Do not invent values: leave \
anything not shown as null.
"""


def system_prompt_for(client_names: list[str], base: str = SYSTEM_PROMPT) -> str:
    """``base`` plus who the receiving company is. Constant per deployment, so
    the cached prefix stays identical across calls."""
    if not client_names:
        return base
    return base + (
        f"\nThe receiving company — the payee, never the payer — is "
        f"{', '.join(client_names)}. An advice may print it as \"Vendor Name\" or "
        f"\"Beneficiary\"; the payer is the other party.\n"
    )


def build_mapping_message(table: LineTable) -> str:
    lines = ["Columns:"]
    lines += [f"{i} = {cell}" for i, cell in enumerate(table.header)]
    lines += ["", "First rows:"]
    lines += [" | ".join(row) for row in table.rows[:3]]
    return "\n".join(lines)


def build_header_message(
    sender_address: str, subject: str, raw_extractions: list[dict], table: LineTable
) -> str:
    trimmed: list[dict] = []
    for si, raw in enumerate(raw_extractions):
        tables = list(raw.get("tables") or [])
        if si == table.source_index and table.table_index < len(tables):
            shown: list[list[str]] = [table.header, *table.rows[:3]]
            hidden = len(table.rows) - 3
            if hidden > 0:
                shown.append([f"[... {hidden} more rows read separately ...]"])
            if table.total_row is not None:
                shown.append(table.total_row)
            tables[table.table_index] = shown
        trimmed.append({**raw, "tables": tables})
    return build_user_message(sender_address, subject, trimmed)
```
(Import `LineTable` under `TYPE_CHECKING` from `ar_pipeline.tables.mapping` to keep this module free of import-time dependencies.)

`ar_pipeline/normalize/normalizer.py`: move the body of `normalize_email` after the `llm_client.parse` call into
```python
def build_payments(email_id: str, out: NormalizerOutput) -> list[NormalizedPayment]:
    """One validated ``NormalizedPayment`` per draft that survives schema construction."""
```
(same logic as today, including the skipped-draft handling and payment-0 raw dump; `raw = out.model_dump(mode="json")` computed inside). `normalize_email` gains `client_names: list[str] | None = None` and becomes:
```python
    user = build_user_message(sender_address, subject, raw_extractions)
    out = llm_client.parse(
        system=system_prompt_for(client_names or []), user=user, output_model=NormalizerOutput
    )
    return out, build_payments(email_id, out)
```

`ar_pipeline/normalize/stub_client.py`:
- `_payer_name(text)` skips client names:
```python
def _is_client(name: str, clients: list[str]) -> bool:
    return bool(payer_slug(name)) and any(
        payer_slug(c) and not payers_differ(name, c) for c in clients
    )


def _payer_name(text: str) -> str:
    clients = get_settings().client_name_list()
    m = _PAYER_LABEL_RE.search(text)
    if m and not _is_client(m.group(1).strip(), clients):
        return m.group(1).strip()
    lines = [ln.strip() for ln in text.strip().splitlines()]
    # a labeled name that is our own company: the payer is the letterhead (top)
    ordered = lines if m else list(reversed(lines))
    for candidate in ordered:
        if _looks_like_company_name(candidate) and not _is_client(candidate, clients):
            return candidate
    return _PAYER_UNKNOWN
```
- In `StubLLMClient.parse`, before the existing body:
```python
        if output_model is MappingOutput:
            header = re.findall(r"^(\d+) = (.*)$", user, re.M)
            cells = [cell for _, cell in header]
            return output_model.model_validate(
                mapping_output(keyword_mapping(cells)).model_dump()
            )
        if output_model is HeaderOutput:
            draft = _draft(user)
            return output_model.model_validate({
                "is_remittance": _is_remittance(user),
                "notes": _NOTE,
                "payer_name": draft["payer_name"],
                "payment_reference": draft["payment_reference"],
                "payment_reference_type": draft["payment_reference_type"],
                "currency": draft["currency"],
                "confidence": draft["confidence"],
            })
```
(imports: `get_settings`, `payer_slug`, `payers_differ`, `keyword_mapping`, `mapping_output`, `MappingOutput`, `HeaderOutput`.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/normalize tests/tables -q`
Expected: PASS. Existing stub tests that rely on a labeled payer still pass because `CLIENT_NAMES` is empty in tests.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/normalize tests/normalize
git commit -m "feat(pdfs): mapping and header prompts, payer guard, offline stub mapper"
```

---

### Task 8: One recheck path for every flag

**Files:**
- Create: `ar_pipeline/normalize/recheck.py`
- Modify: `ar_pipeline/normalize/service.py`, `ar_pipeline/review/service.py` (`save_edits`), `ar_pipeline/ledger/checks.py` (`refresh_pending_flags`)
- Test: `tests/normalize/test_recheck.py`

**Interfaces — Consumes:** `totals_flags` (Task 5), `payer_flags` (Task 7), `check_against_ledger` (Task 6), flag constants in `threads/dedupe.py`.
**Produces:** `TRUNCATED_FLAG` (moved here; `normalize.service` re-imports it so existing imports keep working); `sticky_flags(flags: list[str], *, key_changed: bool) -> list[str]`; `context_flags(session, ext: Extraction, payload) -> list[str]` = totals + payer + ledger (incl. adjustments); `recheck(session, ext, payload, *, key_changed: bool) -> list[str]`; `refresh_pending(session) -> int`.

- [ ] **Step 1: Write the failing tests**

`tests/normalize/test_recheck.py`:
```python
from __future__ import annotations

from ar_pipeline.ledger.checks import refresh_pending_flags
from ar_pipeline.normalize.recheck import TRUNCATED_FLAG, recheck, sticky_flags
from ar_pipeline.normalize.payer import FLAG_PAYER_IS_CLIENT
from ar_pipeline.schema.canonical import RemittancePayload
from ar_pipeline.tables.totals import FLAG_TOTALS
from ar_pipeline.threads.dedupe import FLAG_HISTORICAL, FLAG_POSSIBLE_DUPLICATE

HIST = f"{FLAG_HISTORICAL} payment is older than the newest message"
DUP = f"{FLAG_POSSIBLE_DUPLICATE} of payment 123 (same payer, amount and date)"


def test_sticky_flags():
    flags = [HIST, TRUNCATED_FLAG, DUP, "draft 1: schema validation failed: x", "line 0: other"]
    assert sticky_flags(flags, key_changed=False) == flags[:4]
    assert sticky_flags(flags, key_changed=True) == [HIST, TRUNCATED_FLAG,
                                                     "draft 1: schema validation failed: x"]


def test_refresh_keeps_sticky_flags(db_session, seed_pending):
    _email, ext = seed_pending()
    ext.validation_flags = [HIST, "line 0: stale ledger flag"]
    db_session.flush()
    refresh_pending_flags(db_session)
    assert HIST in ext.validation_flags
    assert "line 0: stale ledger flag" not in ext.validation_flags


def test_recheck_adds_totals_and_payer_flags(db_session, seed_pending, monkeypatch):
    from ar_pipeline.config import get_settings

    monkeypatch.setenv("CLIENT_NAMES", "Acme Corp")
    get_settings.cache_clear()
    _email, ext = seed_pending()
    ext.read_info = {"path": "table", "document_totals": {"words": "500.00"}}
    payload = RemittancePayload.model_validate(ext.canonical)
    flags = recheck(db_session, ext, payload, key_changed=False)
    assert FLAG_PAYER_IS_CLIENT in flags
    assert any(f.startswith(FLAG_TOTALS) for f in flags)
    get_settings.cache_clear()
```
(`seed_pending` lives in `tests/review/conftest.py`; re-export it for `tests/normalize` the same way `tests/threads/conftest.py` does.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/normalize/test_recheck.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement**

`ar_pipeline/normalize/recheck.py`:
```python
"""Every flag a payment carries, recomputed the same way wherever it is
(re)checked: at normalization, on a reviewer's save, and after a CSV import.
Flags that only the original read can know (history, truncation, dropped
drafts, and duplicate warnings while the key is unchanged) are kept."""

from __future__ import annotations

import re

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ar_pipeline.config import get_settings
from ar_pipeline.db.models import Extraction
from ar_pipeline.ledger.checks import check_against_ledger
from ar_pipeline.normalize.payer import payer_flags
from ar_pipeline.normalize.validators import validate_payload
from ar_pipeline.schema.canonical import RemittancePayload
from ar_pipeline.tables.totals import totals_flags
from ar_pipeline.threads.dedupe import FLAG_HISTORICAL, KEY_FLAG_PREFIXES

TRUNCATED_FLAG = "header: content was truncated — check nothing is missing"
_DRAFT_FLAG_RE = re.compile(r"^draft\s+\d+:")


def sticky_flags(flags: list[str], *, key_changed: bool) -> list[str]:
    return [
        f
        for f in flags
        if f == TRUNCATED_FLAG
        or f.startswith(FLAG_HISTORICAL)
        or _DRAFT_FLAG_RE.match(f)
        or (not key_changed and f.startswith(KEY_FLAG_PREFIXES))
    ]


def context_flags(session: Session, ext: Extraction, payload: RemittancePayload) -> list[str]:
    document = (ext.read_info or {}).get("document_totals")
    return (
        totals_flags(payload, document)
        + payer_flags(payload, get_settings().client_name_list())
        + check_against_ledger(session, payload)
    )


def recheck(
    session: Session, ext: Extraction, payload: RemittancePayload, *, key_changed: bool
) -> list[str]:
    kept = sticky_flags(list(ext.validation_flags or []), key_changed=key_changed)
    fresh = validate_payload(payload) + context_flags(session, ext, payload)
    return kept + [f for f in fresh if f not in kept]


def refresh_pending(session: Session) -> int:
    """Recompute the flags of every pending remittance (after a CSV import or a
    backfill). Returns how many rows changed."""
    changed = 0
    pending = session.scalars(
        select(Extraction).where(
            Extraction.status == "pending_review", Extraction.is_remittance.is_(True)
        )
    )
    for ext in pending:
        try:
            payload = RemittancePayload.model_validate(ext.canonical or {})
        except ValidationError:
            continue
        new = recheck(session, ext, payload, key_changed=False)
        if new != list(ext.validation_flags or []):
            ext.validation_flags = new
            changed += 1
    session.flush()
    return changed
```

`ar_pipeline/ledger/checks.py`: replace the body of `refresh_pending_flags` with a local import (avoids an import cycle) and delete the now-unused `_DRAFT_FLAG_RE` / `validate_payload` imports if nothing else uses them:
```python
def refresh_pending_flags(session: Session) -> int:
    """Recompute the flags of every pending remittance against the current ledger."""
    from ar_pipeline.normalize.recheck import refresh_pending

    return refresh_pending(session)
```

`ar_pipeline/normalize/service.py`: delete the local `TRUNCATED_FLAG = …` and add `from ar_pipeline.normalize.recheck import TRUNCATED_FLAG, context_flags`. In `_route`, replace
`row.validation_flags = list(row.validation_flags) + check_against_ledger(session, payload)` with
`row.validation_flags = list(row.validation_flags) + context_flags(session, row, payload)`; drop the unused `check_against_ledger` import.

`ar_pipeline/review/service.py` `save_edits`: replace the `kept = [...]` / `recomputed = ...` / `ext.validation_flags = ...` lines with
```python
    ext.validation_flags = recheck(session, ext, payload, key_changed=key_changed)
```
(keep `shown = list(ext.validation_flags or [])` before it and `ext.canonical = normalised` as today; drop imports that become unused.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/normalize tests/review tests/ledger tests/threads -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/normalize ar_pipeline/review/service.py ar_pipeline/ledger/checks.py tests/normalize
git commit -m "refactor(pdfs): one recheck path for flags; CSV refresh keeps sticky flags"
```

---

### Task 9: Table reader, saved mappings, normalization integration

**Files:**
- Create: `ar_pipeline/tables/store.py`, `ar_pipeline/tables/reader.py`
- Modify: `ar_pipeline/normalize/service.py`
- Test: `tests/tables/test_reader.py`, `tests/normalize/test_service.py` (only if an existing test breaks — see Step 3 note)

**Interfaces — Consumes:** everything from Tasks 4, 5, 6, 7.
**Produces:**
- `store.py`: `load_mapping(session, signature) -> ColumnMap | None`; `save_mapping(session, signature, header, cols, payer_slug) -> None` (insert or replace, `uses = 1`); `touch_mapping(session, signature) -> None` (`uses += 1`, `last_used_at = now()`); `discard_mapping(session, signature) -> None`.
- `reader.py`: `TableRead(output: NormalizerOutput, payments: list[NormalizedPayment], read_info: dict)`; `read_by_table(session, *, email_id: str, sender: str, subject: str, raws: list[dict], llm_client, client_names: list[str]) -> TableRead | None`.
- `Extraction.read_info` written for every normalized payment: `{"path": "table" | "ai", "mapping": "saved" | "learned" | None, "document_totals": dict[str, str]}`.

- [ ] **Step 1: Write the failing tests**

`tests/tables/test_reader.py`:
```python
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from ar_pipeline.db.models import ColumnMapping
from ar_pipeline.extract.pdf import extract_pdf
from ar_pipeline.normalize.stub_client import StubLLMClient
from ar_pipeline.tables.mapping import find_line_table, header_signature
from ar_pipeline.tables.models import HeaderOutput, MappingOutput
from ar_pipeline.tables.reader import read_by_table
from tests.tables.advice_pdf import NET, build_advice_pdf


class Counting:
    def __init__(self):
        self.inner = StubLLMClient()
        self.models: list[type] = []

    def parse(self, *, system, user, output_model):
        self.models.append(output_model)
        return self.inner.parse(system=system, user=user, output_model=output_model)


def _read(session, llm):
    raws = [extract_pdf(build_advice_pdf()).to_payload()]
    return read_by_table(session, email_id="e", sender="ap@ourco.com", subject="FW: advice",
                         raws=raws, llm_client=llm, client_names=["Acme Metals"])


def test_first_read_learns_and_saves_the_mapping(db_session):
    llm = Counting()
    read = _read(db_session, llm)
    assert read is not None
    assert llm.models == [MappingOutput, HeaderOutput]
    payload = read.payments[0].payload
    assert len(payload.line_items) == 20 and payload.header.total_paid_amount == NET
    assert read.read_info["path"] == "table" and read.read_info["mapping"] == "learned"
    assert read.read_info["document_totals"]["words"] == "6809764.78"
    assert db_session.scalar(select(ColumnMapping.uses)) == 1


def test_second_read_uses_the_saved_mapping(db_session):
    _read(db_session, Counting())
    llm = Counting()
    read = _read(db_session, llm)
    assert llm.models == [HeaderOutput]
    assert read.read_info["mapping"] == "saved"
    assert db_session.scalar(select(ColumnMapping.uses)) == 2


def test_corrupted_mapping_is_discarded_and_relearned(db_session):
    _read(db_session, Counting())
    row = db_session.scalar(select(ColumnMapping))
    row.columns = {**row.columns, "amount_paid": 3, "invoice_amount": 6}  # swapped
    db_session.flush()
    llm = Counting()
    read = _read(db_session, llm)
    assert read is not None and read.read_info["mapping"] == "learned"
    assert MappingOutput in llm.models
    # the bad row was deleted and a fresh one learned in its place
    assert db_session.scalar(select(ColumnMapping.columns))["amount_paid"] == 6


def test_no_line_table_means_full_ai_read(db_session):
    llm = Counting()
    read = read_by_table(db_session, email_id="e", sender="s", subject="s",
                         raws=[{"text": "Paid Rs 500 vide UTR ABCD1234567", "tables": []}],
                         llm_client=llm, client_names=[])
    assert read is None and llm.models == []
```
Append to `tests/tables/test_reader.py` an integration test through `normalize_one` — build an email whose single pdf source holds the advice:
```python
def test_normalize_one_reads_the_advice_by_table(db_session, monkeypatch):
    from datetime import UTC, datetime

    from ar_pipeline.config import get_settings
    from ar_pipeline.db.models import (
        Email, EmailMessage, Extraction, ExtractionSource, Invoice, RawExtraction,
    )
    from ar_pipeline.ledger.matching import number_key
    from ar_pipeline.normalize.service import normalize_one

    monkeypatch.setenv("CLIENT_NAMES", "Acme Metals")
    get_settings.cache_clear()
    db_session.add(Invoice(invoice_number="CBB2510004516", number_key=number_key("CBB2510004516"),
                           payer_name="Continental Bus Body Builders", amount=Decimal("300.00"),
                           currency="INR", source="books", paid_before_import=Decimal("0")))
    email = Email(internet_message_id="<adv@x>", sender_address="ap@ourco.com",
                  sender_domain="ourco.com", subject="FW: advice",
                  received_at=datetime(2026, 1, 20, tzinfo=UTC), status="extracted")
    db_session.add(email)
    db_session.flush()
    msg = EmailMessage(email_id=email.id, position=0, raw_header="", is_internal=True,
                       carries_attachments=True, status="new")
    db_session.add(msg)
    db_session.flush()
    src = ExtractionSource(email_id=email.id, kind="pdf_text", ref="att-1",
                           email_message_id=msg.id)
    db_session.add(src)
    db_session.flush()
    db_session.add(RawExtraction(extraction_source_id=src.id,
                                 payload=extract_pdf(build_advice_pdf()).to_payload()))
    db_session.flush()

    assert normalize_one(db_session, email, Counting()) == 1
    ext = db_session.scalar(select(Extraction).where(Extraction.email_id == email.id))
    lines = ext.canonical["line_items"]
    assert lines[1]["applies_to"] == "CBB2510004583"     # exact, same payment
    assert lines[2]["applies_to"] == "CBB2510004516"     # exact, your books
    assert "line 10: adjustment of ₹4,100.00 — did you mean CBB25100035016?" in ext.validation_flags
    assert "line 3: adjustment of ₹11,200.00 — which invoice does it reduce?" in ext.validation_flags
    assert not any(f.startswith("header: doesn't match") for f in ext.validation_flags)
    assert ext.read_info["path"] == "table"
    assert ext.canonical["header"]["payer_name"] == "CONTINENTAL BUS BODY BUILDERS LIMITED"
    get_settings.cache_clear()
```
Adjust `ExtractionSource`/`RawExtraction` constructor fields to the model if they differ (e.g. `extractor_version`).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/tables/test_reader.py -q`
Expected: FAIL — modules do not exist.

- [ ] **Step 3: Implement**

`ar_pipeline/tables/store.py`:
```python
"""Saved column mappings, one per table header layout (spec §4.2)."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ar_pipeline.db.models import ColumnMapping
from ar_pipeline.tables.mapping import ColumnMap


def load_mapping(session: Session, signature: str) -> ColumnMap | None:
    columns = session.scalar(
        select(ColumnMapping.columns).where(ColumnMapping.signature == signature)
    )
    return {str(k): int(v) for k, v in columns.items()} if columns else None


def save_mapping(
    session: Session, signature: str, header: list[str], cols: ColumnMap, payer_slug: str
) -> None:
    values = {"header": list(header), "columns": dict(cols), "payer_slug": payer_slug or None,
              "uses": 1, "last_used_at": func.now()}
    session.execute(
        pg_insert(ColumnMapping)
        .values(id=uuid.uuid4(), signature=signature, **values)
        .on_conflict_do_update(index_elements=["signature"], set_=values)
    )
    session.flush()


def touch_mapping(session: Session, signature: str) -> None:
    session.execute(
        update(ColumnMapping)
        .where(ColumnMapping.signature == signature)
        .values(uses=ColumnMapping.uses + 1, last_used_at=func.now())
    )
    session.flush()


def discard_mapping(session: Session, signature: str) -> None:
    session.execute(delete(ColumnMapping).where(ColumnMapping.signature == signature))
    session.flush()
```
(`save_mapping`, `touch_mapping` and `discard_mapping` run Core statements, so an already-loaded `ColumnMapping` ORM object is stale afterwards; tests read the columns back with a fresh `select`, as above.)

`ar_pipeline/tables/reader.py`:
```python
"""Read a payment advice through its line table (spec §4.2): map the columns
once per header layout, apply the mapping to every row in code, ask the AI only
for the header, and accept the read only when it agrees with the document's
own totals. Returns None whenever the full-AI read should run instead."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from ar_pipeline.ledger.matching import payer_slug
from ar_pipeline.normalize.llm_client import LLMClient
from ar_pipeline.normalize.normalizer import (
    NormalizedPayment,
    NormalizerOutput,
    PaymentDraft,
    build_payments,
)
from ar_pipeline.normalize.prompt import (
    HEADER_SYSTEM_PROMPT,
    MAPPING_SYSTEM_PROMPT,
    build_header_message,
    build_mapping_message,
    system_prompt_for,
)
from ar_pipeline.tables.mapping import (
    ColumnMap,
    LineTable,
    MappedTable,
    MappingError,
    apply_mapping,
    find_line_table,
    header_signature,
    validate_mapping,
)
from ar_pipeline.tables.models import HeaderOutput, MappingOutput
from ar_pipeline.tables.store import discard_mapping, load_mapping, save_mapping, touch_mapping
from ar_pipeline.tables.totals import document_totals, totals_flags


@dataclass(frozen=True)
class TableRead:
    output: NormalizerOutput
    payments: list[NormalizedPayment]
    read_info: dict


def _learn(table: LineTable, llm_client: LLMClient) -> ColumnMap | None:
    out = llm_client.parse(
        system=MAPPING_SYSTEM_PROMPT, user=build_mapping_message(table), output_model=MappingOutput
    )
    return validate_mapping(out, table.header)


def _output(header: HeaderOutput, mapped: MappedTable) -> NormalizerOutput:
    total = sum((li.amount_paid for li in mapped.lines), Decimal("0"))
    draft = PaymentDraft(
        payer_name=header.payer_name or "",
        payer_id=header.payer_id,
        payment_reference=header.payment_reference,
        payment_reference_type=header.payment_reference_type,
        payment_date=header.payment_date,
        payment_method=header.payment_method,
        currency=header.currency,
        total_paid_amount=total,
        line_items=list(mapped.lines),
        vendor_guess=header.vendor_guess,
        confidence=header.confidence,
    )
    note = f"Rows read from the table in code ({len(mapped.lines)} lines)."
    notes = f"{header.notes} | {note}" if header.notes else note
    return NormalizerOutput(is_remittance=True, notes=notes, payments=[draft])


def read_by_table(
    session: Session,
    *,
    email_id: str,
    sender: str,
    subject: str,
    raws: list[dict],
    llm_client: LLMClient,
    client_names: list[str],
) -> TableRead | None:
    table = find_line_table(raws)
    if table is None:
        return None
    signature = header_signature(table.header)
    texts = [str(r.get("text") or "") for r in raws]
    saved = load_mapping(session, signature)
    header: HeaderOutput | None = None
    for origin in (["saved"] if saved is not None else []) + ["learned"]:
        cols = saved if origin == "saved" else _learn(table, llm_client)
        if cols is None:
            return None
        try:
            mapped = apply_mapping(table, cols)
        except MappingError:
            if origin == "saved":
                discard_mapping(session, signature)
                continue
            return None
        if header is None:
            header = llm_client.parse(
                system=system_prompt_for(client_names, HEADER_SYSTEM_PROMPT),
                user=build_header_message(sender, subject, raws, table),
                output_model=HeaderOutput,
            )
            if not header.is_remittance:
                return None
        out = _output(header, mapped)
        payments = build_payments(email_id, out)
        if len(payments) != 1:
            return None
        document = document_totals(mapped.column_totals, texts)
        if totals_flags(payments[0].payload, document):
            if origin == "saved":
                discard_mapping(session, signature)
                continue
            return None
        if origin == "learned":
            save_mapping(session, signature, table.header, cols,
                         payer_slug(payments[0].payload.header.payer_name))
        else:
            touch_mapping(session, signature)
        return TableRead(out, payments, {"path": "table", "mapping": origin,
                                         "document_totals": document})
    return None
```

`ar_pipeline/normalize/service.py` `_normalize_group` — replace the `normalize_email(...)` call and `truncated = …` with:
```python
    client_names = get_settings().client_name_list()
    read = read_by_table(
        session, email_id=str(email.id), sender=sender, subject=email.subject, raws=raws,
        llm_client=llm_client, client_names=client_names,
    )
    if read is not None:
        out, payments, read_info = read.output, read.payments, read.read_info
        truncated = False
    else:
        out, payments = normalize_email(
            email_id=str(email.id), sender_address=sender, subject=email.subject,
            raw_extractions=raws, llm_client=llm_client, client_names=client_names,
        )
        texts = [str(r.get("text") or "") for r in raws]
        # the amount in words names one payment; a multi-payment read can't use it
        read_info = {"path": "ai", "mapping": None,
                     "document_totals": document_totals({}, texts) if len(payments) == 1 else {}}
        truncated = is_truncated(sender, email.subject, raws)
```
and for each payment row:
```python
            payload = resolve_adjustments(session, payment.payload)
            row = Extraction(
                ...
                canonical=payload.model_dump(mode="json"),
                ...
                read_info=read_info,
                ...
            )
```
(imports: `read_by_table`, `document_totals`, `resolve_adjustments`.)

Note on existing tests: a fixture whose sources hold one table with ≥ 6 data rows now takes the table path first. If an existing `tests/normalize` or `tests/pipeline` test that drives `FakeLLMClient(response=NormalizerOutput(...))` breaks for that reason, change only that test to queue `MappingOutput(is_line_table=False)` before its `NormalizerOutput` (`FakeLLMClient(responses=[...])`), and list each such test in the report.

- [ ] **Step 4: Run tests**

Run: `uv run pytest -q`
Expected: PASS (whole suite).

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/tables ar_pipeline/normalize/service.py tests
git commit -m "feat(pdfs): read advices through their line table with saved mappings"
```

---

### Task 10: Review screen — adjustment strip, "Use …", totals badges

**Files:**
- Modify: `ar_pipeline/review/service.py`, `ar_pipeline/review/app.py`, `ar_pipeline/review/templates/detail.html`, `ar_pipeline/review/static/review.css`
- Test: `tests/review/test_adjustment_review.py`

**Interfaces — Consumes:** `LineLedger.kind/adjustment_suggestions` (Task 6), `totals_status` (Task 5), `adjustment_suggestions` (Task 6), `read_info` (Task 9).
**Produces:** `DetailView.totals: str | None = None` (`match`/`mismatch`/`not_found`, None when not recorded) and `DetailView.read_path: str | None = None`; `use_adjustment_target(session, extraction_id, user, line_index: int, number: str) -> str`; route `POST /review/{extraction_id}/use-adjustment` (form `line_index`, `number`) → redirect to the detail page with flash `Adjustment now reduces <number>`.

- [ ] **Step 1: Write the failing tests**

`tests/review/test_adjustment_review.py`:
```python
from __future__ import annotations

ADJ = {
    "invoice_number": "251000458DISCO", "invoice_date": None, "invoice_amount": "0",
    "deductions": [{"type": "discount", "amount": "5.00", "reason": None}],
    "amount_paid": "-5.00", "kind": "adjustment", "applies_to": None,
}


def _with_adjustment(seed_pending, db_session, read_info=None):
    email, ext = seed_pending()
    canonical = dict(ext.canonical)
    first = {**canonical["line_items"][0], "invoice_number": "CBB2510004583"}
    canonical["line_items"] = [first, ADJ]
    canonical["header"] = {**canonical["header"], "total_paid_amount": "85.00"}
    ext.canonical = canonical
    ext.read_info = read_info
    db_session.flush()
    return email, ext


def test_detail_shows_adjustment_strip_and_use_button(client, db_session, seed_pending):
    _email, ext = _with_adjustment(seed_pending, db_session)
    page = client.get(f"/review/{ext.id}").text
    assert "Adjustment" in page and "Which invoice does it reduce?" in page
    assert "Use CBB2510004583" in page
    assert 'name="line_items[1].kind" value="adjustment"' in page


def test_use_adjustment_sets_applies_to(client, db_session, seed_pending):
    _email, ext = _with_adjustment(seed_pending, db_session)
    r = client.post(f"/review/{ext.id}/use-adjustment",
                    data={"line_index": "1", "number": "CBB2510004583"})
    assert r.status_code == 303 and "Adjustment%20now%20reduces" in r.headers["location"]
    db_session.refresh(ext)
    assert ext.canonical["line_items"][1]["applies_to"] == "CBB2510004583"


def test_use_adjustment_refuses_a_number_that_is_not_suggested(client, db_session, seed_pending):
    _email, ext = _with_adjustment(seed_pending, db_session)
    client.post(f"/review/{ext.id}/use-adjustment", data={"line_index": "1", "number": "X1"})
    db_session.refresh(ext)
    assert ext.canonical["line_items"][1]["applies_to"] is None


def test_totals_badges(client, db_session, seed_pending):
    _email, ext = _with_adjustment(
        seed_pending, db_session,
        read_info={"path": "table", "mapping": "learned", "document_totals": {"words": "85.00"}},
    )
    page = client.get(f"/review/{ext.id}").text
    assert "Totals match the document" in page and "Rows read from the table" in page
    ext.read_info = {"path": "ai", "mapping": None, "document_totals": {"words": "999.00"}}
    db_session.flush()
    assert "Totals don't match the document" in client.get(f"/review/{ext.id}").text


def test_saving_an_old_payload_records_no_format_noise(client, db_session, seed_pending):
    from sqlalchemy import select

    from ar_pipeline.db.models import ExtractionEdit

    _email, ext = seed_pending()  # canonical without kind / schema_version
    form = {"header.payer_name": "Acme Corp", "header.total_paid_amount": "90.00",
            "header.currency": "INR", "header.payment_reference": "UTR-1",
            "header.payment_reference_type": "utr", "header.payment_date": "2026-09-05",
            "header.payment_method": "RTGS",
            "line_items[0].invoice_number": "INV-1", "line_items[0].invoice_date": "2026-08-01",
            "line_items[0].invoice_amount": "100.00", "line_items[0].amount_paid": "90.00",
            "line_items[0].kind": "invoice",
            "line_items[0].deductions[0].type": "tds",
            "line_items[0].deductions[0].amount": "10.00",
            "line_items[0].deductions[0].reason": "194Q"}
    client.post(f"/review/{ext.id}/save", data=form)
    paths = list(db_session.scalars(select(ExtractionEdit.field_path)
                                    .where(ExtractionEdit.extraction_id == ext.id)))
    assert not any(p.endswith(".kind") or p.endswith(".applies_to") for p in paths)
```
Check the save route's real path and form field names in `app.py` / `detail.html` and adapt `test_saving_an_old_payload_records_no_format_noise` to them.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/review/test_adjustment_review.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`ar_pipeline/review/service.py`:
- `DetailView` gains `totals: str | None = None` and `read_path: str | None = None` (last). In `load_detail`:
```python
    totals = read_path = None
    if ext.read_info:
        read_path = ext.read_info.get("path")
        try:
            payload = RemittancePayload.model_validate(ext.canonical or {})
            totals = totals_status(payload, ext.read_info.get("document_totals"))
        except ValidationError:
            totals = None
```
and pass `totals=totals, read_path=read_path`.
- `save_edits`: before computing `edits`, normalise the stored side through the schema so pre-v2 rows don't produce `kind` / `applies_to` / `schema_version` audit rows:
```python
    try:
        stored_view = RemittancePayload.model_validate(stored).model_dump(mode="json")
    except ValidationError:
        stored_view = stored
    edits = canonical_diff(
        {"header": stored_view.get("header", {}), "line_items": stored_view.get("line_items", [])},
        {"header": normalised["header"], "line_items": normalised["line_items"]},
    )
```
- New:
```python
def use_adjustment_target(
    session: Session, extraction_id: uuid.UUID, user: User, line_index: int, number: str
) -> str:
    """Reviewer confirmed which invoice an adjustment reduces — through the
    normal edit path so it is audited and the checks re-run."""
    from ar_pipeline.ledger.adjustments import adjustment_suggestions

    ext = _require_pending(session.get(Extraction, extraction_id))
    try:
        payload = RemittancePayload.model_validate(ext.canonical or {})
    except ValidationError as exc:
        raise ReviewError("this payment can't be read — edit it first") from exc
    if not 0 <= line_index < len(payload.line_items):
        raise ReviewError("no such line on this payment")
    if number not in adjustment_suggestions(session, payload, line_index):
        raise ReviewError(f"{number} is not a suggested invoice for this adjustment")
    canonical = payload.model_dump(mode="json")
    canonical["line_items"][line_index]["applies_to"] = number
    save_edits(session, extraction_id, user,
               {"header": canonical["header"], "line_items": canonical["line_items"]},
               approve=False)
    return number
```

`ar_pipeline/review/app.py` — next to `use_invoice_action`, the same shape:
```python
@router.post("/{extraction_id}/use-adjustment")
def use_adjustment_action(
    extraction_id: uuid.UUID,
    line_index: int = Form(...),
    number: str = Form(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.review.service import ReviewError, use_adjustment_target

    try:
        chosen = use_adjustment_target(session, extraction_id, user, line_index, number)
    except ReviewError as exc:
        return RedirectResponse(f"/review/{extraction_id}?flash={quote(str(exc))}", status_code=303)
    return RedirectResponse(
        f"/review/{extraction_id}?flash={quote(f'Adjustment now reduces {chosen}')}",
        status_code=303,
    )
```

`ar_pipeline/review/templates/detail.html`:
- Under the existing flags/badges line add:
```html
{% if view.read_path == "table" %}<span class="badge">Rows read from the table</span>{% endif %}
{% if view.totals == "match" %}<span class="badge ok">Totals match the document</span>
{% elif view.totals == "mismatch" %}<span class="badge bad">Totals don't match the document</span>
{% elif view.totals == "not_found" %}<span class="badge">No totals in the document to check</span>{% endif %}
```
- In each line fieldset: legend `Line {{ li_idx }}{% if li.get("kind") == "adjustment" %} — Adjustment{% endif %}`; first child `<input type="hidden" name="line_items[{{ li_idx }}].kind" value="{{ li.get('kind') or 'invoice' }}">`; for adjustments a visible `<label>reduces invoice <input type="text" name="line_items[{{ li_idx }}].applies_to" value="{{ li.get('applies_to') or '' }}"></label>`.
- Inside the ledger strip, branch first on adjustments:
```html
{% if led.kind == "adjustment" %}
  {% if led.invoice_id %}
    Reduces <a href="/review/invoices/{{ led.invoice_id }}">{{ led.invoice_number }}</a>
    &middot; outstanding {{ led.outstanding|money(led.currency) }}
    &middot; <strong>after this {{ led.after_this|money(led.currency) }}</strong>
  {% elif led.invoice_number %}
    Reduces {{ led.invoice_number }} — a new invoice in this payment.
  {% else %}
    Which invoice does it reduce?
    {% for number in led.adjustment_suggestions %}
    <button type="submit" form="useAdjustment{{ li_idx }}_{{ loop.index0 }}" class="btn-quiet">Use {{ number }}</button>
    {% else %}Type it in "reduces invoice" below.{% endfor %}
  {% endif %}
{% elif led.invoice_id %} … existing branches unchanged …
```
- Next to the `useInvoice…` hidden forms:
```html
{% for led in line_ledgers or [] %}{% set li_idx = loop.index0 %}
  {% for number in led.adjustment_suggestions %}
  <form method="post" action="/review/{{ view.extraction.id }}/use-adjustment" id="useAdjustment{{ li_idx }}_{{ loop.index0 }}" hidden>
    <input type="hidden" name="line_index" value="{{ li_idx }}">
    <input type="hidden" name="number" value="{{ number }}">
  </form>
  {% endfor %}
{% endfor %}
```
- The "+ line item" JS clone must carry a `kind` hidden input with value `invoice` (check the cloning script in `static/` and add the field to the template row it clones).

`ar_pipeline/review/static/review.css`: `.lineitem legend { font-variant-numeric: tabular-nums; }` is enough; reuse `.badge`, `.badge.ok`, `.badge.bad` already defined (add `.badge.bad` only if absent, using the existing error colour token).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/review -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/review tests/review/test_adjustment_review.py
git commit -m "feat(pdfs): adjustment strip, Use button and totals badges on the review screen"
```

---

### Task 11: End-to-end offline run, docs

**Files:**
- Modify: `README.md`, `docs/superpowers/specs/2026-10-03-threads-and-multi-invoice-pdfs-design.md` (append the corrections below)
- Test: `tests/tables/test_end_to_end.py`

**Interfaces — Consumes:** everything.

- [ ] **Step 1: Write the end-to-end test**

`tests/tables/test_end_to_end.py` — an email with the synthetic advice as a PDF attachment goes `new → classified → extracted → review` through `advance_once` with the offline stub; a second advice with the same layout uses the saved mapping:
```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from ar_pipeline.db.models import Attachment, Email, Extraction
from ar_pipeline.normalize.stub_client import StubLLMClient
from ar_pipeline.pipeline.advance import advance_once
from ar_pipeline.storage import LocalBlobStore
from ar_pipeline.tables.models import HeaderOutput, MappingOutput
from tests.extract.vision_fake import FakeVisionExtractor
from tests.tables.advice_pdf import NET, build_advice_pdf


class Counting:
    def __init__(self):
        self.inner = StubLLMClient()
        self.models: list[type] = []

    def parse(self, *, system, user, output_model):
        self.models.append(output_model)
        return self.inner.parse(system=system, user=user, output_model=output_model)


@pytest.fixture
def store(tmp_path):
    return LocalBlobStore(str(tmp_path))


def _advice_email(session, store, n: int) -> Email:
    data = build_advice_pdf()
    email = Email(
        internet_message_id=f"<advice-{n}@fixture>", sender_address="ap@ourco.com",
        sender_domain="ourco.com", subject="FW: Payment Advice",
        received_at=datetime(2026, 1, 20, tzinfo=UTC),
        body_html="", body_text="Please find attached the payment advice.", status="new",
    )
    session.add(email)
    session.flush()
    att = Attachment(email_id=email.id, filename="advice.pdf", content_type="application/pdf",
                     size=len(data), blob_url="", sha256="0" * 64)
    session.add(att)
    session.flush()
    att.blob_url = store.put(f"{email.id}/{att.id}/advice.pdf", data)
    session.flush()
    return email


def _run(session, store, llm) -> None:
    for _ in range(3):
        advance_once(session, store, FakeVisionExtractor(), llm)


def test_advice_end_to_end_offline(db_session, store, monkeypatch):
    from ar_pipeline.config import get_settings

    monkeypatch.setenv("CLIENT_NAMES", "Acme Metals")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    get_settings.cache_clear()

    first = _advice_email(db_session, store, 1)
    llm = Counting()
    _run(db_session, store, llm)
    db_session.refresh(first)
    assert first.status == "review"
    ext = db_session.scalar(select(Extraction).where(Extraction.email_id == first.id))
    assert len(ext.canonical["line_items"]) == 20
    assert Decimal(ext.canonical["header"]["total_paid_amount"]) == NET
    assert ext.canonical["header"]["payer_name"] == "CONTINENTAL BUS BODY BUILDERS LIMITED"
    assert ext.canonical["line_items"][1]["applies_to"] == "CBB2510004583"
    assert ext.read_info["path"] == "table"
    assert not any(f.startswith("header: doesn't match") for f in ext.validation_flags)
    assert llm.models.count(MappingOutput) == 1

    second = _advice_email(db_session, store, 2)
    llm2 = Counting()
    _run(db_session, store, llm2)
    db_session.refresh(second)
    assert MappingOutput not in llm2.models and HeaderOutput in llm2.models
    get_settings.cache_clear()
```
If the cover-note guard leaves the body text as an extra source for this email, keep the test as written — the table path reads only the single qualifying table, whatever other sources exist.

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/tables/test_end_to_end.py -q`
Expected: PASS (Tasks 1–10 complete). If it fails, fix the cause in the owning module, not in the test.

- [ ] **Step 3: Docs**

README — a short "Multi-invoice payment advices" section matching the README's existing tone and heading level: what the table path does (columns mapped once per layout, every row copied in code, the header read by AI), the totals check and its badge, adjustment lines and the "Use …" button, the payer guard and `CLIENT_NAMES`, and that `scripts/dev_db.py migrate` applies migration 0006 (no backfill needed — old payloads read as `schema_version` 2 with `kind: invoice`).

Spec — append the "Planning-time corrections (Stage 2)" section below, verbatim.

- [ ] **Step 4: Full verification**
- `uv run ruff check ar_pipeline tests && uv run ruff format --check ar_pipeline tests`
- `uv run mypy ar_pipeline`
- `uv run pytest -q` → all pass.

- [ ] **Step 5: Commit**
```bash
git add README.md docs/superpowers/specs tests/tables/test_end_to_end.py
git commit -m "feat(pdfs): end-to-end offline advice run, docs"
```

---

## Planning-time corrections (Stage 2, 2026-10-04)

Found while planning against the code; these supersede spec §4 where they differ.

1. **Saved mappings are keyed by the table's header layout, in a new `column_mapping` table, not per payer in `vendor.column_hints`.** The payer is only known after the header is read, so a per-payer lookup cannot run first; the header signature (cells lower-cased, punctuation removed) identifies the advice format, and the totals check catches a mapping reused on the wrong document. The payer slug is stored for display only.
2. **The header is always read by one small AI call** (the table cut to its header, 3 rows and the Total row). Rows are never re-typed by the AI, which was the cost and output-cap problem; reading the header "by pattern" was dropped as fragile.
3. **Adjustment rows are recognised in code, not by an AI-returned rule:** a row is an adjustment when its net is negative, or its gross is empty/zero and its adjustment column has a value. Its type is `discount` when its number contains "DISC", else `debit_note`.
4. **"One digit off" means one inserted or missing digit only.** A substituted digit is usually the neighbouring invoice (…4515 vs …4516) and would be a wrong suggestion.
5. **The column-total check runs on the table path only** (it needs the mapping); every single-payment read also gets the amount-in-words check. A multi-payment message gets no totals check.
6. **On the table path `total_paid_amount` is the sum of the lines** and header deductions are empty; the totals check compares it with the document.
7. **The table path needs one clear table:** exactly one table in the message's sources with ≥ 6 data rows, ≥ 3 columns and ≥ 2 mostly-numeric columns. Smaller tables stay on the full-AI path, which handles them well.
8. **Adjustment matching looks at this payment's invoice lines first, then the ledger.** An adjustment is posted to the ledger only when its target invoice exists there (after this payment's own invoice lines are posted); an adjustment is never used to create an invoice.
9. **CSV refresh keeps sticky flags.** `refresh_pending_flags` used to drop history, truncation and duplicate flags (a Stage 1 gap); all flag recomputation now goes through `normalize/recheck.py`.
10. **Totals status is derived, not stored:** `extraction.read_info` stores the document's printed totals; match / mismatch is recomputed whenever the payment is shown or rechecked, so a reviewer's correction clears the mismatch.
