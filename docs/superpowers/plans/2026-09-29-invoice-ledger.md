# Invoice Ledger & Invoices Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track every approved payment against its invoice so partial payments show progress instead of being flagged, overpayments/duplicates/mismatches are caught, and an Invoices tab (with CSV import of the client's open invoices) shows balances live.

**Architecture:** Two new tables (`invoice`, `invoice_payment`) in the pipeline DB. A new `ar_pipeline/ledger/` package holds pure matching/money helpers, balance math, posting (writes a payment row per approved line, hooked into the shared `approve_and_queue`), ledger checks (run in `normalize_one` and `save_edits`, which have a session), CSV import, and read-side queries for the UI. The review app gets an Invoices tab, an invoice detail page, a per-line ledger strip on the review screen, and a "Use INV-…" button.

**Tech Stack:** Python 3.12, FastAPI + Jinja2, SQLAlchemy 2.0 on Postgres (embedded pgserver in tests), Alembic, pytest, ruff (line length 100), mypy.

**Spec:** `docs/superpowers/specs/2026-09-29-invoice-ledger-design.md`

## Global Constraints

- Tolerance for all money comparisons: `Decimal("0.02")` (same as `validators._TOLERANCE`).
- Money columns: `Numeric(14, 2)`. Never use float for money.
- Table names are singular: `invoice`, `invoice_payment`.
- Invoice `source` values: exactly `"books"` or `"email"`. UI labels: *your books* / *unverified*.
- Invoice status values: `open`, `partially_paid`, `paid`, `overpaid`.
- Every ledger flag string starts with `line {i}: ` so `review/service.py::_LINE_FLAG_RE` (`^line\s+(\d+):\s*(.+)$`) pins it to the line.
- `CHECK_VERSION` becomes `"3"`.
- CSV limits: 1 MB (1_000_000 bytes), 5,000 data rows. Required columns `invoice_number`, `invoice_amount`; optional `payer_name`, `invoice_date` (YYYY-MM-DD), `currency` (default INR), `outstanding_amount`.
- Near-match suggestions: at most 3, same payer first. Never applied automatically.
- Payer-comparison stop words: `pvt, private, ltd, limited, llp, inc, co, the`.
- INR amounts render with Indian grouping and ₹ (`₹1,00,000.00`); other currencies as `USD 4,250.00`.
- Ledger rows are never deleted (no un-approve path exists).
- Commands: tests `uv run pytest <path> -q`; lint `uv run ruff check ar_pipeline tests`; format check `uv run ruff format --check ar_pipeline tests`; types `uv run mypy ar_pipeline`. Full suite takes ~18 min — run it in the background.
- Commit only when the user has asked for commits; each task's commit step is written for when they have. Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `ar_pipeline/db/models.py` (modify) | `Invoice`, `InvoicePayment` ORM models |
| `migrations/versions/0004_invoice_ledger.py` (create) | create/drop the two tables |
| `ar_pipeline/ledger/__init__.py` (create) | package marker |
| `ar_pipeline/ledger/money.py` (create) | `format_money` |
| `ar_pipeline/ledger/matching.py` (create) | `number_key`, `is_near_match`, `near_matches`, `payers_differ` — pure |
| `ar_pipeline/ledger/balance.py` (create) | `TOLERANCE`, `line_settled`, `status_for`, `InvoiceBalance`, `balances`, `balance_for`, `awaiting_by_key` |
| `ar_pipeline/ledger/posting.py` (create) | `find_invoice`, `post_extraction`, `backfill` |
| `ar_pipeline/ledger/checks.py` (create) | `check_against_ledger` |
| `ar_pipeline/ledger/csv_import.py` (create) | `import_open_invoices`, `ImportResult`, `CsvImportError`, `TEMPLATE_CSV` |
| `ar_pipeline/ledger/queries.py` (create) | read models for the UI: `InvoiceRow`, `list_invoice_rows`, `summarize`, `filter_rows`, `InvoiceDetail`, `invoice_detail`, `LineLedger`, `line_ledgers` |
| `ar_pipeline/pipeline/routing.py` (modify) | `approve_and_queue` posts to the ledger |
| `ar_pipeline/normalize/validators.py` (modify) | check 1 only flags over-payment; `CHECK_VERSION = "3"` |
| `ar_pipeline/normalize/service.py` (modify) | per-row ledger check before auto-approve |
| `ar_pipeline/review/service.py` (modify) | ledger check in `save_edits`, `ApprovalBlocked`, `use_invoice` |
| `ar_pipeline/review/app.py` (modify) | invoice routes, `use-invoice` route, `money` filter, `line_ledgers` in detail page |
| `ar_pipeline/review/templates/invoices.html`, `_invoices_rows.html`, `invoice_detail.html` (create) | Invoices tab + detail |
| `ar_pipeline/review/templates/base.html`, `detail.html` (modify) | nav link; per-line ledger strip |
| `ar_pipeline/review/static/review.css` (modify) | progress bar, ledger strip, upload form |
| `ar_pipeline/cli.py` (modify) | `ledger-backfill` command |
| `demo/open-invoices.csv`, `demo/open-invoices-template.csv` (create) | demo data |
| `tests/ledger/…` (create) | tests per module |

---

### Task 1: Ledger tables (models + migration)

**Files:**
- Modify: `ar_pipeline/db/models.py`
- Create: `migrations/versions/0004_invoice_ledger.py`
- Create: `tests/ledger/__init__.py` (empty), `tests/ledger/test_models.py`

**Interfaces:**
- Produces: `Invoice`, `InvoicePayment`, `INVOICE_SOURCES` in `ar_pipeline.db.models`.

- [ ] **Step 1: Write the failing test**

`tests/ledger/test_models.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from ar_pipeline.db.models import Email, Extraction, Invoice, InvoicePayment


def _extraction(db_session) -> Extraction:
    email = Email(
        internet_message_id="m-ledger-model",
        sender_address="a@b.com",
        sender_domain="b.com",
        subject="s",
        received_at=datetime(2026, 9, 1, tzinfo=UTC),
        status="review",
    )
    db_session.add(email)
    db_session.flush()
    ext = Extraction(email_id=email.id, canonical={}, status="approved")
    db_session.add(ext)
    db_session.flush()
    return ext


def test_invoice_and_payment_round_trip(db_session):
    inv = Invoice(
        invoice_number="INV-1", number_key="INV1", amount=Decimal("100.00"), source="books"
    )
    db_session.add(inv)
    db_session.flush()
    ext = _extraction(db_session)
    pay = InvoicePayment(
        invoice_id=inv.id,
        extraction_id=ext.id,
        line_index=0,
        amount_paid=Decimal("25.00"),
        deductions_total=Decimal("0.00"),
        settled=Decimal("25.00"),
        currency="INR",
    )
    db_session.add(pay)
    db_session.flush()
    db_session.refresh(inv)
    assert inv.currency == "INR"
    assert inv.paid_before_import == Decimal("0.00")
    assert pay.payment_reference is None


def test_number_key_is_unique(db_session):
    db_session.add(Invoice(invoice_number="INV-1", number_key="INV1", amount=Decimal("1"), source="books"))
    db_session.flush()
    db_session.add(Invoice(invoice_number="inv/1", number_key="INV1", amount=Decimal("1"), source="email"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_one_payment_row_per_extraction_line(db_session):
    inv = Invoice(invoice_number="INV-1", number_key="INV1", amount=Decimal("100"), source="books")
    db_session.add(inv)
    db_session.flush()
    ext = _extraction(db_session)
    for _ in range(2):
        db_session.add(
            InvoicePayment(
                invoice_id=inv.id, extraction_id=ext.id, line_index=0,
                amount_paid=Decimal("1"), deductions_total=Decimal("0"),
                settled=Decimal("1"), currency="INR",
            )
        )
    with pytest.raises(IntegrityError):
        db_session.flush()
```
(Wrap any line over 100 chars when running ruff.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/ledger/test_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'Invoice'`.

- [ ] **Step 3: Add the models**

In `ar_pipeline/db/models.py`: add `Date` to the `sqlalchemy` import list and `from datetime import date, datetime` (replacing `from datetime import datetime`). After `DELIVERY_STATUSES` add:
```python
INVOICE_SOURCES = ("books", "email")
```
Append at the end of the file:
```python
class Invoice(Base):
    """One invoice in the client's books — from their CSV (``books``) or first
    seen in an approved remittance (``email``, shown as unverified)."""

    __tablename__ = "invoice"

    id: Mapped[uuid.UUID] = _uuid_pk()
    invoice_number: Mapped[str] = mapped_column(Text)
    number_key: Mapped[str] = mapped_column(Text)
    payer_name: Mapped[str | None] = mapped_column(Text)
    invoice_date: Mapped[date | None] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    source: Mapped[str] = mapped_column(String(10))
    paid_before_import: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("number_key", name="uq_invoice_number_key"),
        CheckConstraint(_in("source", INVOICE_SOURCES), name="ck_invoice_source"),
    )


class InvoicePayment(Base):
    """One approved line item applied to an invoice. Written only at approval;
    never deleted (there is no un-approve path)."""

    __tablename__ = "invoice_payment"

    id: Mapped[uuid.UUID] = _uuid_pk()
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoice.id"), index=True)
    extraction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extraction.id"), index=True)
    line_index: Mapped[int] = mapped_column(Integer)
    amount_paid: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    deductions_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    settled: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    payment_reference: Mapped[str | None] = mapped_column(Text)
    payment_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "extraction_id", "line_index", name="uq_invoice_payment_extraction_line"
        ),
    )
```

- [ ] **Step 4: Write the migration**

`migrations/versions/0004_invoice_ledger.py`:
```python
"""invoice ledger: invoice + invoice_payment

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-29 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "invoice",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("invoice_number", sa.Text(), nullable=False),
        sa.Column("number_key", sa.Text(), nullable=False),
        sa.Column("payer_name", sa.Text(), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("paid_before_import", sa.Numeric(14, 2), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("source IN ('books', 'email')", name="ck_invoice_source"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("number_key", name="uq_invoice_number_key"),
    )
    op.create_table(
        "invoice_payment",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("invoice_id", sa.UUID(), nullable=False),
        sa.Column("extraction_id", sa.UUID(), nullable=False),
        sa.Column("line_index", sa.Integer(), nullable=False),
        sa.Column("amount_paid", sa.Numeric(14, 2), nullable=False),
        sa.Column("deductions_total", sa.Numeric(14, 2), nullable=False),
        sa.Column("settled", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payment_reference", sa.Text(), nullable=True),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["extraction_id"], ["extraction.id"]),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoice.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "extraction_id", "line_index", name="uq_invoice_payment_extraction_line"
        ),
    )
    op.create_index(op.f("ix_invoice_payment_invoice_id"), "invoice_payment", ["invoice_id"])
    op.create_index(op.f("ix_invoice_payment_extraction_id"), "invoice_payment", ["extraction_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_invoice_payment_extraction_id"), table_name="invoice_payment")
    op.drop_index(op.f("ix_invoice_payment_invoice_id"), table_name="invoice_payment")
    op.drop_table("invoice_payment")
    op.drop_table("invoice")
```
(Existing migrations are excluded from ruff line length if they already exceed it; if ruff complains, wrap the long `created_at`/`updated_at` column lines.)

- [ ] **Step 5: Run tests (models + migration drift)**

Run: `uv run pytest tests/ledger/test_models.py tests/db -q`
Expected: PASS. `tests/db/test_migrations.py::test_no_model_migration_drift` runs `alembic check` and fails if the migration and models disagree — fix whichever side is off until it passes.

- [ ] **Step 6: Apply to the dev database**

Run: `uv run python scripts/dev_db.py migrate`
Expected: alembic upgrades to `0004` with no error.

- [ ] **Step 7: Commit**
```bash
git add ar_pipeline/db/models.py migrations/versions/0004_invoice_ledger.py tests/ledger
git commit -m "feat(ledger): invoice and invoice_payment tables"
```

---

### Task 2: Money formatting and invoice-number/payer matching (pure)

**Files:**
- Create: `ar_pipeline/ledger/__init__.py` (docstring only: `"""Invoice ledger: balances, checks, CSV import."""`)
- Create: `ar_pipeline/ledger/money.py`, `ar_pipeline/ledger/matching.py`
- Test: `tests/ledger/test_money.py`, `tests/ledger/test_matching.py`

**Interfaces:**
- Produces:
  - `format_money(amount: Decimal, currency: str = "INR") -> str`
  - `number_key(raw: str) -> str`
  - `is_near_match(line_number: str, invoice_number: str) -> bool`
  - `near_matches(line_number: str, invoices: Iterable[Invoice], payer_name: str | None, limit: int = 3) -> list[Invoice]`
  - `payers_differ(a: str | None, b: str | None) -> bool`

- [ ] **Step 1: Write the failing tests**

`tests/ledger/test_money.py`:
```python
from decimal import Decimal

import pytest

from ar_pipeline.ledger.money import format_money


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        (Decimal("100000"), "INR", "₹1,00,000.00"),
        (Decimal("12345678.5"), "INR", "₹1,23,45,678.50"),
        (Decimal("25"), "INR", "₹25.00"),
        (Decimal("999"), "INR", "₹999.00"),
        (Decimal("-5"), "INR", "-₹5.00"),
        (Decimal("4250"), "USD", "USD 4,250.00"),
    ],
)
def test_format_money(amount, currency, expected):
    assert format_money(amount, currency) == expected
```

`tests/ledger/test_matching.py`:
```python
from decimal import Decimal

import pytest

from ar_pipeline.db.models import Invoice
from ar_pipeline.ledger.matching import is_near_match, near_matches, number_key, payers_differ


@pytest.mark.parametrize(
    "raw", ["INV-2026-0412", "INV/2026/0412", "inv 2026 0412", "INV.2026_0412", " inv-2026-0412 "]
)
def test_number_key_ignores_case_spaces_and_separators(raw):
    assert number_key(raw) == "INV20260412"


def test_number_key_of_blank_is_empty():
    assert number_key("   ") == ""


@pytest.mark.parametrize(
    ("line", "invoice"),
    [
        ("MST-2026-07550", "MST-2026-7550"),  # rule 1: leading zeros per number group
        ("7550", "MST-2026-7550"),  # rule 2: bare suffix, >= 4 chars
        ("MST/2026/780", "MST-2026-7801"),  # rule 3: one deletion
        ("MST-2026-755O", "MST-2026-7550"),  # rule 3: one substitution (letter O)
    ],
)
def test_near_match_rules(line, invoice):
    assert is_near_match(line, invoice)


@pytest.mark.parametrize(
    ("line", "invoice"),
    [
        ("MST-2026-7550", "MST-2026-7550"),  # exact is not "near"
        ("550", "MST-2026-7550"),  # suffix shorter than 4
        ("MST-2026-7505", "MST-2026-7550"),  # two edits
        ("AB12", "AB13"),  # one edit but shorter than 6
        ("", "MST-2026-7550"),
    ],
)
def test_not_near_match(line, invoice):
    assert not is_near_match(line, invoice)


def _inv(number: str, payer: str | None) -> Invoice:
    return Invoice(
        invoice_number=number, number_key=number_key(number), payer_name=payer,
        amount=Decimal("1"), source="books",
    )


def test_near_matches_puts_same_payer_first_and_caps_at_three():
    invoices = [
        _inv("A-1-7550", "Other Co"),
        _inv("B-2-7550", "Meridian Steel Pvt Ltd"),
        _inv("C-3-7550", None),
        _inv("D-4-7550", "Other Co"),
        _inv("X-9-9999", "Meridian Steel"),
    ]
    found = near_matches("7550", invoices, "Meridian Steel")
    assert [i.invoice_number for i in found] == ["B-2-7550", "A-1-7550", "C-3-7550"]


@pytest.mark.parametrize(
    ("a", "b", "differ"),
    [
        ("Meridian Steel", "Meridian Steel Pvt Ltd", False),
        ("MERIDIAN STEEL PRIVATE LIMITED", "meridian steel", False),
        ("Meridian Steel Traders", "Meridian Steel", False),  # one contains the other
        ("Arcadia Foods", "Meridian Steel", True),
        (None, "Meridian Steel", False),  # only compared when both present
        ("", "Meridian Steel", False),
        ("The Co", "Meridian Steel", False),  # nothing left after stop words
    ],
)
def test_payers_differ(a, b, differ):
    assert payers_differ(a, b) is differ
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/ledger/test_money.py tests/ledger/test_matching.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ar_pipeline.ledger'`.

- [ ] **Step 3: Implement**

`ar_pipeline/ledger/money.py`:
```python
"""Money display: INR with Indian digit grouping (₹1,00,000.00), others as
``USD 4,250.00``."""

from __future__ import annotations

from decimal import Decimal


def _indian_grouping(whole: str) -> str:
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


def format_money(amount: Decimal, currency: str = "INR") -> str:
    q = Decimal(amount).quantize(Decimal("0.01"))
    sign = "-" if q < 0 else ""
    whole, frac = f"{abs(q):.2f}".split(".")
    if currency == "INR":
        return f"{sign}₹{_indian_grouping(whole)}.{frac}"
    return f"{sign}{currency} {int(whole):,}.{frac}"
```

`ar_pipeline/ledger/matching.py`:
```python
"""Invoice-number and payer matching. Pure: no DB, no I/O.

``number_key`` is the automatic match. ``is_near_match`` only ever produces a
*suggestion* for a reviewer — never an automatic match (spec §4).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from ar_pipeline.db.models import Invoice

_SEPARATORS_RE = re.compile(r"[\s\-/._]+")
_DIGITS_RE = re.compile(r"\d+")
_PAYER_STOPWORDS = {"pvt", "private", "ltd", "limited", "llp", "inc", "co", "the"}


def number_key(raw: str) -> str:
    return _SEPARATORS_RE.sub("", raw).upper()


def _zero_insensitive(raw: str) -> str:
    # per number group of the ORIGINAL string: stripping separators first would
    # merge "2026" and "07550" into one run and hide the leading zero.
    tokens = [t for t in _SEPARATORS_RE.split(raw.upper()) if t]
    return "".join(_DIGITS_RE.sub(lambda m: m.group(0).lstrip("0") or "0", t) for t in tokens)


def _one_edit_apart(a: str, b: str) -> bool:
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1 :]


def is_near_match(line_number: str, invoice_number: str) -> bool:
    line_key, inv_key = number_key(line_number), number_key(invoice_number)
    if not line_key or line_key == inv_key:
        return False
    if _zero_insensitive(line_number) == _zero_insensitive(invoice_number):
        return True
    if len(line_key) >= 4 and inv_key.endswith(line_key):
        return True
    return len(line_key) >= 6 and len(inv_key) >= 6 and _one_edit_apart(line_key, inv_key)


def _payer_words(name: str | None) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"[^\w\s]", " ", name.lower())
    return " ".join(w for w in cleaned.split() if w not in _PAYER_STOPWORDS)


def payers_differ(a: str | None, b: str | None) -> bool:
    """True only when both names are present and clearly different companies."""
    wa, wb = _payer_words(a), _payer_words(b)
    if not wa or not wb:
        return False
    return not (wa == wb or wa in wb or wb in wa)


def near_matches(
    line_number: str, invoices: Iterable[Invoice], payer_name: str | None, limit: int = 3
) -> list[Invoice]:
    found = [inv for inv in invoices if is_near_match(line_number, inv.invoice_number)]

    def rank(inv: Invoice) -> tuple[int, str]:
        same = bool(payer_name and inv.payer_name and not payers_differ(payer_name, inv.payer_name))
        return (0 if same else 1, inv.invoice_number)

    return sorted(found, key=rank)[:limit]
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/ledger/test_money.py tests/ledger/test_matching.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/ledger tests/ledger/test_money.py tests/ledger/test_matching.py
git commit -m "feat(ledger): money formatting and invoice/payer matching"
```

---

### Task 3: Balance math

**Files:**
- Create: `ar_pipeline/ledger/balance.py`
- Test: `tests/ledger/test_balance.py`, `tests/ledger/conftest.py`

**Interfaces:**
- Consumes: `Invoice`, `InvoicePayment`, `Extraction` (Task 1); `number_key` (Task 2).
- Produces:
  - `TOLERANCE: Decimal`
  - `line_settled(line: dict) -> Decimal` — `amount_paid + Σ deductions[].amount` from a canonical line dict
  - `status_for(amount: Decimal, paid: Decimal) -> str`
  - `@dataclass(frozen=True) InvoiceBalance(invoice: Invoice, paid: Decimal, outstanding: Decimal, status: str)`
  - `balances(session, invoices: Sequence[Invoice]) -> list[InvoiceBalance]`
  - `balance_for(session, invoice: Invoice) -> InvoiceBalance`
  - `awaiting_by_key(session, exclude: uuid.UUID | None = None) -> dict[str, Decimal]`
- Test fixtures produced in `tests/ledger/conftest.py` (used by Tasks 3–10): `make_invoice`, `make_extraction`, `post_payment`.

- [ ] **Step 1: Write shared fixtures**

`tests/ledger/conftest.py`:
```python
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ar_pipeline.db.models import Email, Extraction, Invoice, InvoicePayment
from ar_pipeline.ledger.matching import number_key


def canonical_for(
    *,
    invoice_number: str = "INV-1",
    invoice_amount: str = "100.00",
    amount_paid: str = "100.00",
    tds: str | None = None,
    payer_name: str = "Acme Corp",
    currency: str = "INR",
    reference: str | None = "UTR-1",
) -> dict:
    deductions = [{"type": "tds", "amount": tds, "reason": None}] if tds else []
    total = Decimal(amount_paid)
    return {
        "envelope": {
            "extraction_id": str(uuid.uuid4()),
            "source_email_id": "email-x",
            "payment_index": 0,
            "vendor_guess": None,
            "extracted_at": datetime(2026, 9, 9, tzinfo=UTC).isoformat(),
            "reviewed_by": None,
        },
        "header": {
            "payer_name": payer_name,
            "payer_id": None,
            "payment_reference": reference,
            "payment_reference_type": "utr",
            "payment_date": "2026-09-20",
            "payment_method": "NEFT",
            "currency": currency,
            "total_paid_amount": str(total),
            "deductions": [],
        },
        "line_items": [
            {
                "invoice_number": invoice_number,
                "invoice_date": None,
                "invoice_amount": invoice_amount,
                "deductions": deductions,
                "amount_paid": amount_paid,
            }
        ],
    }


@pytest.fixture
def make_invoice(db_session):
    def _make(
        number: str = "INV-1",
        amount: str = "100.00",
        *,
        source: str = "books",
        payer: str | None = "Acme Corp",
        currency: str = "INR",
        paid_before_import: str = "0",
    ) -> Invoice:
        inv = Invoice(
            invoice_number=number,
            number_key=number_key(number),
            payer_name=payer,
            amount=Decimal(amount),
            currency=currency,
            source=source,
            paid_before_import=Decimal(paid_before_import),
        )
        db_session.add(inv)
        db_session.flush()
        return inv

    return _make


@pytest.fixture
def make_extraction(db_session):
    def _make(status: str = "pending_review", subject: str = "payment", **canon) -> Extraction:
        email = Email(
            internet_message_id=f"m-{uuid.uuid4()}",
            sender_address="ap@payer.example",
            sender_domain="payer.example",
            subject=subject,
            received_at=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
            status="review",
        )
        db_session.add(email)
        db_session.flush()
        ext = Extraction(
            email_id=email.id,
            canonical=canonical_for(**canon),
            confidence=Decimal("0.95"),
            is_remittance=True,
            validation_flags=[],
            status=status,
        )
        db_session.add(ext)
        db_session.flush()
        return ext

    return _make


@pytest.fixture
def post_payment(db_session, make_extraction):
    """Record an approved payment of ``settled`` directly (bypasses posting)."""

    def _post(
        invoice: Invoice,
        settled: str,
        *,
        currency: str = "INR",
        reference: str = "UTR-9",
        payment_date: date | None = None,
    ):
        ext = make_extraction(status="approved")
        row = InvoicePayment(
            invoice_id=invoice.id,
            extraction_id=ext.id,
            line_index=0,
            amount_paid=Decimal(settled),
            deductions_total=Decimal("0"),
            settled=Decimal(settled),
            currency=currency,
            payment_reference=reference,
            payment_date=payment_date,
        )
        db_session.add(row)
        db_session.flush()
        return row

    return _post
```

- [ ] **Step 2: Write the failing tests**

`tests/ledger/test_balance.py`:
```python
from decimal import Decimal

import pytest

from ar_pipeline.ledger.balance import awaiting_by_key, balance_for, line_settled, status_for


@pytest.mark.parametrize(
    ("amount", "paid", "status"),
    [
        ("100", "0", "open"),
        ("100", "25", "partially_paid"),
        ("100", "100", "paid"),
        ("100", "99.99", "paid"),  # within tolerance
        ("100", "100.02", "paid"),
        ("100", "105", "overpaid"),
    ],
)
def test_status_for(amount, paid, status):
    assert status_for(Decimal(amount), Decimal(paid)) == status


def test_line_settled_counts_deductions():
    line = {"amount_paid": "90.00", "deductions": [{"type": "tds", "amount": "10.00"}]}
    assert line_settled(line) == Decimal("100.00")


def test_installments_add_up_to_overpaid(make_invoice, post_payment, db_session):
    inv = make_invoice(amount="100")
    for amt in ("25", "50", "30"):
        post_payment(inv, amt)
    bal = balance_for(db_session, inv)
    assert bal.paid == Decimal("105")
    assert bal.outstanding == Decimal("-5")
    assert bal.status == "overpaid"


def test_paid_before_import_counts(make_invoice, post_payment, db_session):
    inv = make_invoice(amount="100", paid_before_import="40")
    post_payment(inv, "10")
    bal = balance_for(db_session, inv)
    assert bal.paid == Decimal("50")
    assert bal.status == "partially_paid"


def test_other_currency_payment_is_excluded(make_invoice, post_payment, db_session):
    inv = make_invoice(amount="100")
    post_payment(inv, "60", currency="USD")
    assert balance_for(db_session, inv).status == "open"


def test_awaiting_by_key_sums_pending_lines_only(make_extraction, db_session):
    a = make_extraction(invoice_number="INV-1", invoice_amount="100", amount_paid="40")
    make_extraction(invoice_number="inv/1", invoice_amount="100", amount_paid="10", tds="5")
    make_extraction(status="approved", invoice_number="INV-1", amount_paid="99")
    assert awaiting_by_key(db_session) == {"INV1": Decimal("55")}
    assert awaiting_by_key(db_session, exclude=a.id) == {"INV1": Decimal("15")}
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/ledger/test_balance.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ar_pipeline.ledger.balance'`.

- [ ] **Step 4: Implement**

`ar_pipeline/ledger/balance.py`:
```python
"""Invoice balances, always derived from payment rows — never stored."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Extraction, Invoice, InvoicePayment
from ar_pipeline.ledger.matching import number_key

TOLERANCE = Decimal("0.02")
_ZERO = Decimal("0")


def _dec(value: object) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else _ZERO
    except InvalidOperation:
        return _ZERO


def line_settled(line: dict) -> Decimal:
    deductions = line.get("deductions") or []
    return _dec(line.get("amount_paid")) + sum((_dec(d.get("amount")) for d in deductions), _ZERO)


def status_for(amount: Decimal, paid: Decimal) -> str:
    outstanding = amount - paid
    if outstanding < -TOLERANCE:
        return "overpaid"
    if abs(outstanding) <= TOLERANCE:
        return "paid"
    if paid == 0:
        return "open"
    return "partially_paid"


@dataclass(frozen=True)
class InvoiceBalance:
    invoice: Invoice
    paid: Decimal
    outstanding: Decimal
    status: str


def balances(session: Session, invoices: Sequence[Invoice]) -> list[InvoiceBalance]:
    if not invoices:
        return []
    ids = [inv.id for inv in invoices]
    rows = session.execute(
        select(InvoicePayment.invoice_id, func.sum(InvoicePayment.settled))
        .join(Invoice, Invoice.id == InvoicePayment.invoice_id)
        .where(InvoicePayment.invoice_id.in_(ids), InvoicePayment.currency == Invoice.currency)
        .group_by(InvoicePayment.invoice_id)
    )
    settled: dict[uuid.UUID, Decimal] = {}
    for invoice_id, total in rows:
        settled[invoice_id] = Decimal(total)
    out = []
    for inv in invoices:
        paid = Decimal(inv.paid_before_import or 0) + settled.get(inv.id, _ZERO)
        out.append(InvoiceBalance(inv, paid, Decimal(inv.amount) - paid, status_for(inv.amount, paid)))
    return out


def balance_for(session: Session, invoice: Invoice) -> InvoiceBalance:
    return balances(session, [invoice])[0]


def awaiting_by_key(session: Session, exclude: uuid.UUID | None = None) -> dict[str, Decimal]:
    """Σ settled of lines on still-pending extractions, by invoice number key.
    Display only — never part of a balance (spec Q3)."""
    query = select(Extraction).where(
        Extraction.status == "pending_review", Extraction.is_remittance.is_(True)
    )
    if exclude is not None:
        query = query.where(Extraction.id != exclude)
    out: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for ext in session.scalars(query):
        for line in (ext.canonical or {}).get("line_items") or []:
            key = number_key(str(line.get("invoice_number") or ""))
            if key:
                out[key] += line_settled(line)
    return dict(out)
```

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest tests/ledger/test_balance.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**
```bash
git add ar_pipeline/ledger/balance.py tests/ledger/conftest.py tests/ledger/test_balance.py
git commit -m "feat(ledger): derived invoice balances and awaiting-review totals"
```

---

### Task 4: Posting approved payments to the ledger

**Files:**
- Create: `ar_pipeline/ledger/posting.py`
- Modify: `ar_pipeline/pipeline/routing.py` (`approve_and_queue`)
- Test: `tests/ledger/test_posting.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `find_invoice(session, invoice_number: str) -> Invoice | None`
  - `post_extraction(session, extraction: Extraction) -> int` — rows written (0 if nothing to post); idempotent
  - `backfill(session) -> tuple[int, int]` — (extractions that posted ≥1 row, rows written)
- `approve_and_queue` now calls `post_extraction` — every approval (auto and human) posts.

- [ ] **Step 1: Write the failing tests**

`tests/ledger/test_posting.py`:
```python
from decimal import Decimal

from sqlalchemy import select

from ar_pipeline.db.models import Invoice, InvoicePayment
from ar_pipeline.ledger.posting import backfill, find_invoice, post_extraction
from ar_pipeline.pipeline.routing import approve_and_queue


def _payments(db_session):
    return db_session.scalars(select(InvoicePayment)).all()


def test_posts_one_row_per_line_against_a_books_invoice(db_session, make_invoice, make_extraction):
    inv = make_invoice("INV-1", "100")
    ext = make_extraction(invoice_number="inv/1", invoice_amount="100", amount_paid="90", tds="10")
    assert post_extraction(db_session, ext) == 1
    (row,) = _payments(db_session)
    assert row.invoice_id == inv.id
    assert (row.amount_paid, row.deductions_total, row.settled) == (
        Decimal("90.00"), Decimal("10.00"), Decimal("100.00"),
    )
    assert row.payment_reference == "UTR-1"
    assert row.currency == "INR"


def test_posting_twice_does_not_double_count(db_session, make_invoice, make_extraction):
    make_invoice("INV-1", "100")
    ext = make_extraction(amount_paid="25", invoice_amount="100")
    post_extraction(db_session, ext)
    assert post_extraction(db_session, ext) == 0
    assert len(_payments(db_session)) == 1


def test_unknown_invoice_is_created_unverified_from_the_stated_amount(db_session, make_extraction):
    ext = make_extraction(invoice_number="ORB-2026-3390", invoice_amount="8400", amount_paid="8400",
                          payer_name="Orbital Components Ltd")
    post_extraction(db_session, ext)
    inv = find_invoice(db_session, "orb 2026 3390")
    assert inv is not None
    assert (inv.source, inv.amount, inv.payer_name) == ("email", Decimal("8400.00"), "Orbital Components Ltd")


def test_second_unverified_posting_reuses_the_invoice(db_session, make_extraction):
    post_extraction(db_session, make_extraction(invoice_number="NEW-1", amount_paid="10", invoice_amount="30"))
    post_extraction(db_session, make_extraction(invoice_number="new/1", amount_paid="20", invoice_amount="30"))
    assert len(db_session.scalars(select(Invoice)).all()) == 1
    assert len(_payments(db_session)) == 2


def test_approve_and_queue_posts(db_session, make_invoice, make_extraction):
    make_invoice("INV-1", "100")
    ext = make_extraction(amount_paid="25", invoice_amount="100")
    approve_and_queue(db_session, ext, reviewed_by="Asha")
    assert len(_payments(db_session)) == 1


def test_not_a_remittance_or_empty_canonical_posts_nothing(db_session, make_extraction):
    ext = make_extraction()
    ext.is_remittance = False
    assert post_extraction(db_session, ext) == 0
    ext2 = make_extraction()
    ext2.canonical = {}
    assert post_extraction(db_session, ext2) == 0


def test_backfill_posts_approved_only_and_is_rerunnable(db_session, make_invoice, make_extraction):
    make_invoice("INV-1", "100")
    make_extraction(status="approved", amount_paid="25", invoice_amount="100")
    make_extraction(status="pending_review", amount_paid="25", invoice_amount="100")
    assert backfill(db_session) == (1, 1)
    assert backfill(db_session) == (0, 0)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/ledger/test_posting.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ar_pipeline.ledger.posting'`.

- [ ] **Step 3: Implement posting**

`ar_pipeline/ledger/posting.py`:
```python
"""Write approved payments into the ledger.

Called from ``pipeline.routing.approve_and_queue`` — the single approval path
shared by auto-approve and human review — so every approval posts exactly once.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Extraction, Invoice, InvoicePayment
from ar_pipeline.ledger.matching import number_key
from ar_pipeline.schema.canonical import RemittancePayload

log = logging.getLogger(__name__)


def find_invoice(session: Session, invoice_number: str) -> Invoice | None:
    key = number_key(invoice_number)
    if not key:
        return None
    return session.scalar(select(Invoice).where(Invoice.number_key == key))


def _get_or_create_email_invoice(
    session: Session, *, invoice_number: str, amount: Decimal, payer_name: str | None,
    currency: str, invoice_date: object,
) -> Invoice:
    key = number_key(invoice_number)
    # insert-or-fetch: two approvals creating the same unverified invoice at once
    # must not collide on uq_invoice_number_key.
    session.execute(
        pg_insert(Invoice)
        .values(
            id=uuid.uuid4(),
            invoice_number=invoice_number.strip(),
            number_key=key,
            payer_name=payer_name or None,
            invoice_date=invoice_date,
            amount=amount,
            currency=currency,
            source="email",
            paid_before_import=Decimal("0"),
        )
        .on_conflict_do_nothing(index_elements=["number_key"])
    )
    invoice = session.scalar(select(Invoice).where(Invoice.number_key == key))
    assert invoice is not None
    return invoice


def post_extraction(session: Session, extraction: Extraction) -> int:
    canonical = extraction.canonical
    if not extraction.is_remittance or not isinstance(canonical, dict) or not canonical:
        return 0
    try:
        payload = RemittancePayload.model_validate(canonical)
    except ValidationError:
        log.warning("extraction %s: canonical does not validate; not posted", extraction.id)
        return 0
    header = payload.header
    posted = 0
    for i, line in enumerate(payload.line_items):
        if not number_key(line.invoice_number):
            continue
        already = session.scalar(
            select(InvoicePayment.id).where(
                InvoicePayment.extraction_id == extraction.id, InvoicePayment.line_index == i
            )
        )
        if already is not None:
            continue
        deductions = sum((d.amount for d in line.deductions), Decimal("0"))
        settled = line.amount_paid + deductions
        invoice = find_invoice(session, line.invoice_number) or _get_or_create_email_invoice(
            session,
            invoice_number=line.invoice_number,
            amount=line.invoice_amount if line.invoice_amount > 0 else settled,
            payer_name=header.payer_name,
            currency=header.currency,
            invoice_date=line.invoice_date,
        )
        session.add(
            InvoicePayment(
                invoice_id=invoice.id,
                extraction_id=extraction.id,
                line_index=i,
                amount_paid=line.amount_paid,
                deductions_total=deductions,
                settled=settled,
                currency=header.currency,
                payment_reference=header.payment_reference,
                payment_date=header.payment_date,
            )
        )
        posted += 1
    session.flush()
    return posted


def backfill(session: Session) -> tuple[int, int]:
    """Post every already-approved extraction. Safe to re-run."""
    approved = session.scalars(
        select(Extraction)
        .where(Extraction.status == "approved")
        .order_by(Extraction.reviewed_at.asc().nulls_last(), Extraction.id.asc())
    )
    extractions = rows = 0
    for ext in approved:
        n = post_extraction(session, ext)
        if n:
            extractions += 1
            rows += n
    return extractions, rows
```

- [ ] **Step 4: Hook into approval**

In `ar_pipeline/pipeline/routing.py`, add the import `from ar_pipeline.ledger.posting import post_extraction` and change the end of `approve_and_queue` to:
```python
    session.add(Delivery(extraction_id=extraction.id, status="pending", next_attempt_at=func.now()))
    session.flush()
    post_extraction(session, extraction)
```
Update the module docstring's first paragraph to mention: "…approve an extraction, post it to the invoice ledger, and queue its delivery…".

- [ ] **Step 5: Run the new tests and everything that approves**

Run: `uv run pytest tests/ledger tests/review tests/normalize tests/pipeline -q`
Expected: PASS. If an existing test approves an extraction whose canonical doesn't validate, `post_extraction` logs and returns 0 — it must not raise.

- [ ] **Step 6: Commit**
```bash
git add ar_pipeline/ledger/posting.py ar_pipeline/pipeline/routing.py tests/ledger/test_posting.py
git commit -m "feat(ledger): post approved payments; backfill"
```

---

### Task 5: Ledger checks, and check 1 stops flagging partial payments

**Files:**
- Modify: `ar_pipeline/normalize/validators.py`
- Create: `ar_pipeline/ledger/checks.py`
- Test: `tests/normalize/test_validators.py` (modify), `tests/ledger/test_checks.py` (create)

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces: `check_against_ledger(session: Session, payload: RemittancePayload) -> list[str]`.

- [ ] **Step 1: Write the failing validator tests**

In `tests/normalize/test_validators.py` change `test_check_version_constant` to assert `"3"`, and add:
```python
def test_underpaying_line_is_not_flagged_here() -> None:
    """A partial payment is judged by the ledger check, not by check 1."""
    flags = validate_payload(
        _payload(
            line_items=[
                LineItem(
                    invoice_number="INV-1",
                    invoice_amount=Decimal("100.00"),
                    amount_paid=Decimal("25.00"),
                )
            ]
        )
    )
    assert flags == []
```

- [ ] **Step 2: Write the failing ledger-check tests**

`tests/ledger/test_checks.py`:
```python
from ar_pipeline.ledger.checks import check_against_ledger
from ar_pipeline.schema.canonical import RemittancePayload
from tests.ledger.conftest import canonical_for


def _flags(db_session, **canon) -> list[str]:
    return check_against_ledger(db_session, RemittancePayload.model_validate(canonical_for(**canon)))


def test_partial_against_a_books_invoice_is_clean(db_session, make_invoice):
    make_invoice("INV-1", "100")
    assert _flags(db_session, invoice_amount="100", amount_paid="25") == []


def test_installment_that_only_describes_the_payment_is_clean(db_session, make_invoice, post_payment):
    inv = make_invoice("INV-1", "100000")
    post_payment(inv, "25000")
    # stub-style line: invoice_amount == amount_paid, i.e. no claim about the invoice total
    assert _flags(db_session, invoice_amount="50000", amount_paid="50000") == []


def test_partial_against_an_unverified_invoice_is_flagged(db_session, make_invoice):
    make_invoice("INV-1", "100", source="email")
    (flag,) = _flags(db_session, invoice_amount="100", amount_paid="25")
    assert flag.startswith("line 0: INV-1")
    assert "partial payment ₹25.00 of ₹100.00" in flag
    assert "not your books" in flag


def test_partial_against_an_unknown_invoice_is_flagged(db_session):
    (flag,) = _flags(db_session, invoice_number="NEW-9", invoice_amount="100", amount_paid="25")
    assert "partial payment ₹25.00 of ₹100.00" in flag


def test_full_payment_on_unknown_invoice_before_any_csv_is_clean(db_session):
    assert _flags(db_session, invoice_number="NEW-9", invoice_amount="100", amount_paid="100") == []


def test_overpayment(db_session, make_invoice, post_payment):
    inv = make_invoice("INV-1", "100")
    post_payment(inv, "25")
    post_payment(inv, "50")
    (flag,) = _flags(db_session, invoice_amount="30", amount_paid="30")
    assert "pays ₹30.00 but only ₹25.00 outstanding" in flag
    assert "overpaid by ₹5.00" in flag


def test_already_paid(db_session, make_invoice, post_payment):
    inv = make_invoice("INV-1", "100")
    post_payment(inv, "100")
    (flag,) = _flags(db_session, invoice_amount="10", amount_paid="10")
    assert flag == "line 0: INV-1 is already fully paid"


def test_amount_differs_from_books(db_session, make_invoice):
    make_invoice("INV-1", "120")
    (flag,) = _flags(db_session, invoice_amount="100", amount_paid="90", tds="5")
    assert flag == "line 0: INV-1 — email says invoice ₹100.00, your books say ₹120.00"


def test_amount_differs_from_earlier_email(db_session, make_invoice):
    make_invoice("INV-1", "120", source="email")
    # the line must claim a total (invoice_amount != settled) for the flag to apply
    flags = _flags(db_session, invoice_amount="100", amount_paid="80")
    assert "line 0: INV-1 — email says invoice ₹100.00, an earlier email said ₹120.00" in flags


def test_possible_duplicate(db_session, make_invoice, post_payment):
    inv = make_invoice("INV-1", "100")
    post_payment(inv, "50", reference="UTR-1")
    flags = _flags(db_session, invoice_amount="50", amount_paid="50", reference="UTR-1")
    assert "line 0: INV-1 — reference UTR-1 for ₹50.00 was already approved" in flags


def test_currency_mismatch(db_session, make_invoice):
    make_invoice("INV-1", "100")
    (flag,) = _flags(db_session, invoice_amount="100", amount_paid="100", currency="USD")
    assert flag == "line 0: INV-1 — payment in USD, invoice in INR; not applied to the balance"


def test_payer_mismatch(db_session, make_invoice):
    make_invoice("INV-1", "100", payer="Meridian Steel Pvt Ltd")
    (flag,) = _flags(db_session, invoice_amount="100", amount_paid="100", payer_name="Arcadia Foods")
    assert flag == (
        "line 0: INV-1 — invoice belongs to Meridian Steel Pvt Ltd; payment is from Arcadia Foods"
    )


def test_near_match_replaces_not_in_books(db_session, make_invoice):
    make_invoice("MST-2026-7801", "100")
    (flag,) = _flags(db_session, invoice_number="MST/2026/780", invoice_amount="100", amount_paid="100")
    assert flag == "line 0: MST/2026/780 not found — did you mean MST-2026-7801?"


def test_not_in_books_once_a_csv_exists(db_session, make_invoice):
    make_invoice("ZZZ-1", "100")
    (flag,) = _flags(db_session, invoice_number="INV-9999", invoice_amount="100", amount_paid="100")
    assert flag == "line 0: INV-9999 isn't in your open invoices"


def test_email_only_invoices_do_not_trigger_not_in_books(db_session, make_invoice):
    make_invoice("ZZZ-1", "100", source="email")
    assert _flags(db_session, invoice_number="INV-9999", invoice_amount="100", amount_paid="100") == []
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/normalize/test_validators.py tests/ledger/test_checks.py -q`
Expected: FAIL — check-version assertion, the underpay test (check 1 still flags it), and `ModuleNotFoundError` for `ar_pipeline.ledger.checks`.

- [ ] **Step 4: Change check 1 and the version**

In `ar_pipeline/normalize/validators.py`: set `CHECK_VERSION = "3"` and replace check 1 with:
```python
    # 1. line net identity — only a line that pays MORE than its own stated
    # invoice is flagged here. Paying less is a partial payment; the ledger
    # check (ledger/checks.py) judges it against the invoice's real balance.
    for i, line in enumerate(line_items):
        sum_ded = sum((d.amount for d in line.deductions), Decimal("0"))
        residual = line.invoice_amount - sum_ded - line.amount_paid
        if residual < -_TOLERANCE:
            flags.append(
                f"line {i} ({line.invoice_number}): invoice {line.invoice_amount} "
                f"- deductions {sum_ded} != amount_paid {line.amount_paid}"
            )
```

- [ ] **Step 5: Implement the ledger check**

`ar_pipeline/ledger/checks.py`:
```python
"""Checks a payment against the invoice ledger (spec §2).

Runs where a DB session exists — ``normalize.service.normalize_one`` and
``review.service.save_edits`` — after the pure ``validate_payload``. Every flag
starts with ``line {i}: `` so the review screen pins it to that line.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Invoice, InvoicePayment
from ar_pipeline.ledger.balance import TOLERANCE, balance_for
from ar_pipeline.ledger.matching import near_matches, number_key, payers_differ
from ar_pipeline.ledger.money import format_money
from ar_pipeline.schema.canonical import RemittancePayload


def _is_duplicate(session: Session, invoice: Invoice, reference: str, amount_paid: Decimal) -> bool:
    return (
        session.scalar(
            select(func.count())
            .select_from(InvoicePayment)
            .where(
                InvoicePayment.invoice_id == invoice.id,
                InvoicePayment.payment_reference == reference,
                InvoicePayment.amount_paid == amount_paid,
            )
        )
        or 0
    ) > 0


def check_against_ledger(session: Session, payload: RemittancePayload) -> list[str]:
    header = payload.header
    books_loaded = (
        session.scalar(select(func.count()).select_from(Invoice).where(Invoice.source == "books"))
        or 0
    ) > 0
    every_invoice: list[Invoice] | None = None
    flags: list[str] = []

    for i, line in enumerate(payload.line_items):
        if not number_key(line.invoice_number):
            continue  # "empty invoice number" is already flagged by validate_payload
        label = f"line {i}: {line.invoice_number}"
        settled = line.amount_paid + sum((d.amount for d in line.deductions), Decimal("0"))
        # the line only claims an invoice total when that total differs from what it
        # settles; an equal pair just describes the payment (spec §2 "Stated vs settled").
        states_total = abs(line.invoice_amount - settled) > TOLERANCE
        invoice = session.scalar(
            select(Invoice).where(Invoice.number_key == number_key(line.invoice_number))
        )

        if invoice is None:
            if every_invoice is None:
                every_invoice = list(session.scalars(select(Invoice)))
            near = near_matches(line.invoice_number, every_invoice, header.payer_name)
            if near:
                names = ", ".join(inv.invoice_number for inv in near)
                flags.append(f"line {i}: {line.invoice_number} not found — did you mean {names}?")
            elif books_loaded:
                flags.append(f"{label} isn't in your open invoices")
            if states_total and settled < line.invoice_amount:
                flags.append(
                    f"{label} — partial payment {format_money(settled, header.currency)} of "
                    f"{format_money(line.invoice_amount, header.currency)}; invoice amount comes "
                    "from this email, not your books"
                )
            continue

        def money(value: Decimal, _cur: str = invoice.currency) -> str:
            return format_money(value, _cur)

        if header.currency != invoice.currency:
            flags.append(
                f"{label} — payment in {header.currency}, invoice in {invoice.currency}; "
                "not applied to the balance"
            )
            continue
        if payers_differ(header.payer_name, invoice.payer_name):
            flags.append(
                f"{label} — invoice belongs to {invoice.payer_name}; "
                f"payment is from {header.payer_name}"
            )
        if states_total and abs(line.invoice_amount - invoice.amount) > TOLERANCE:
            where = "your books say" if invoice.source == "books" else "an earlier email said"
            flags.append(
                f"{label} — email says invoice {money(line.invoice_amount)}, "
                f"{where} {money(invoice.amount)}"
            )
        if header.payment_reference and _is_duplicate(
            session, invoice, header.payment_reference, line.amount_paid
        ):
            flags.append(
                f"{label} — reference {header.payment_reference} for "
                f"{money(line.amount_paid)} was already approved"
            )
        bal = balance_for(session, invoice)
        if bal.status in ("paid", "overpaid"):
            flags.append(f"{label} is already fully paid")
        elif settled > bal.outstanding + TOLERANCE:
            flags.append(
                f"{label} — pays {money(settled)} but only {money(bal.outstanding)} "
                f"outstanding; overpaid by {money(settled - bal.outstanding)}"
            )
        elif settled < bal.outstanding - TOLERANCE and invoice.source == "email":
            flags.append(
                f"{label} — partial payment {money(settled)} of {money(bal.outstanding)} "
                "outstanding; invoice amount comes from an email, not your books"
            )
    return flags
```
Note the unverified-partial test expects `"partial payment ₹25.00 of ₹100.00"` — for an `email` invoice with nothing paid, outstanding is ₹100.00, so the message reads "partial payment ₹25.00 of ₹100.00 outstanding; …", which contains the asserted substring.

- [ ] **Step 6: Run to verify they pass, then the neighbours**

Run: `uv run pytest tests/normalize/test_validators.py tests/ledger -q`
Expected: PASS.
Then: `uv run pytest tests/normalize tests/pipeline tests/review -q`. Any test that asserted an under-payment was flagged by check 1 now fails — change it to expect no check-1 flag (the ledger check is wired in Task 6). Also grep for `CHECK_VERSION` / `"2"` assertions: `grep -rn "CHECK_VERSION" tests` and update to `"3"`.

- [ ] **Step 7: Commit**
```bash
git add ar_pipeline/normalize/validators.py ar_pipeline/ledger/checks.py tests/normalize/test_validators.py tests/ledger/test_checks.py
git commit -m "feat(ledger): ledger checks; partial payments no longer fail check 1"
```

---

### Task 6: Wire checks into routing; re-check at approval

**Files:**
- Modify: `ar_pipeline/normalize/service.py` (auto-approve loop in `normalize_one`)
- Modify: `ar_pipeline/review/service.py` (`save_edits`, new `ApprovalBlocked`)
- Test: `tests/ledger/test_routing.py` (create)

**Interfaces:**
- Consumes: `check_against_ledger` (Task 5), `post_extraction` via `approve_and_queue` (Task 4).
- Produces: `class ApprovalBlocked(ReviewError)` in `ar_pipeline.review.service`. `app.py`'s existing `except ReviewError` in `edit_action` already turns it into a redirect back to the item with the message as flash — no app change needed.

- [ ] **Step 1: Write the failing tests**

`tests/ledger/test_routing.py`:
```python
import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

import ar_pipeline.config as config_module
from ar_pipeline.db.models import Email, Extraction, ExtractionSource, RawExtraction
from ar_pipeline.normalize.normalizer import NormalizerOutput, PaymentDraft
from ar_pipeline.normalize.service import normalize_one
from ar_pipeline.review.auth import User
from ar_pipeline.review.service import ApprovalBlocked, save_edits
from ar_pipeline.schema.canonical import LineItem
from tests.normalize.llm_fake import FakeLLMClient

ASHA = User(name="Asha")


@pytest.fixture
def auto_approve(monkeypatch):
    monkeypatch.setenv("AUTO_APPROVE_MIN_CONFIDENCE", "0.75")
    config_module.get_settings.cache_clear()
    yield
    config_module.get_settings.cache_clear()


def _extracted_email(db_session, mid: str) -> Email:
    email = Email(
        internet_message_id=mid, sender_address="a@b.com", sender_domain="b.com", subject=mid,
        received_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC), status="extracted",
    )
    db_session.add(email)
    db_session.flush()
    src = ExtractionSource(email_id=email.id, kind="body_text", ref="body")
    db_session.add(src)
    db_session.flush()
    db_session.add(RawExtraction(extraction_source_id=src.id, payload={"text": "x", "tables": []}))
    db_session.flush()
    return email


def _output(*paid: str) -> NormalizerOutput:
    return NormalizerOutput(
        is_remittance=True,
        payments=[
            PaymentDraft(
                payer_name="Acme Corp",
                payment_reference=f"UTR-{p}",
                total_paid_amount=Decimal(p),
                line_items=[
                    LineItem(invoice_number="INV-1", invoice_amount=Decimal(p), amount_paid=Decimal(p))
                ],
                confidence=0.95,
            )
            for p in paid
        ],
    )


def _rows(db_session, email):
    return db_session.scalars(
        select(Extraction).where(Extraction.email_id == email.id).order_by(Extraction.id)
    ).all()


def test_installment_on_books_invoice_auto_approves(db_session, make_invoice, auto_approve):
    make_invoice("INV-1", "100")
    email = _extracted_email(db_session, "m-inst-1")
    normalize_one(db_session, email, FakeLLMClient(response=_output("25")))
    (row,) = _rows(db_session, email)
    assert row.status == "approved"
    assert row.validation_flags == []


def test_second_payment_in_the_same_email_sees_the_first(db_session, make_invoice, auto_approve):
    make_invoice("INV-1", "100")
    email = _extracted_email(db_session, "m-race-auto")
    normalize_one(db_session, email, FakeLLMClient(response=_output("60", "50")))
    statuses = sorted((r.status, tuple(r.validation_flags)) for r in _rows(db_session, email))
    assert statuses[0][0] == "approved"
    assert statuses[1][0] == "pending_review"
    assert any("overpaid by ₹10.00" in f for f in statuses[1][1])


def test_reviewer_approval_is_stopped_when_checks_changed(db_session, make_invoice, make_extraction):
    make_invoice("INV-1", "100")
    a = make_extraction(invoice_amount="60", amount_paid="60", reference="UTR-A")
    b = make_extraction(invoice_amount="50", amount_paid="50", reference="UTR-B")
    form = lambda ext: {"header": ext.canonical["header"], "line_items": ext.canonical["line_items"]}  # noqa: E731

    save_edits(db_session, a.id, ASHA, form(a), approve=True)
    assert a.status == "approved"

    with pytest.raises(ApprovalBlocked):
        save_edits(db_session, b.id, ASHA, form(b), approve=True)
    db_session.refresh(b)
    assert b.status == "pending_review"
    assert any("overpaid by ₹10.00" in f for f in b.validation_flags)

    # approving again, having now seen the flag, goes through
    save_edits(db_session, b.id, ASHA, form(b), approve=True)
    assert b.status == "approved"
```
(Check `User`'s constructor in `ar_pipeline/review/auth.py` before running; if it needs other fields, construct it the way `tests/review/test_service.py` does.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/ledger/test_routing.py -q`
Expected: FAIL — `ImportError: cannot import name 'ApprovalBlocked'`.

- [ ] **Step 3: Wire into `normalize_one`**

In `ar_pipeline/normalize/service.py` add imports:
```python
from ar_pipeline.ledger.checks import check_against_ledger
from ar_pipeline.schema.canonical import RemittancePayload
```
Replace the auto-approve block (from `threshold = get_settings().auto_approve_min_confidence` to the end of its `for` loop) with:
```python
    threshold = get_settings().auto_approve_min_confidence
    for row in rows:
        # Ledger flags are computed per row, immediately before that row's
        # routing decision, so a payment approved earlier in this loop already
        # counts toward the invoice balance (spec §2 "Re-check at approval").
        if row.is_remittance and row.canonical:
            payload = RemittancePayload.model_validate(row.canonical)
            row.validation_flags = list(row.validation_flags) + check_against_ledger(
                session, payload
            )
            session.flush()
        if (
            threshold > 0
            and row.is_remittance
            and row.canonical
            and not row.validation_flags
            and row.confidence is not None
            and float(row.confidence) >= threshold
        ):
            approve_and_queue(session, row, reviewed_by=AUTO_REVIEWER)
```

- [ ] **Step 4: Wire into `save_edits` with the approval re-check**

In `ar_pipeline/review/service.py`: import `from ar_pipeline.ledger.checks import check_against_ledger`; after the `ReviewError` class add:
```python
class ApprovalBlocked(ReviewError):
    """Approval stopped because the checks found something the reviewer
    hadn't been shown yet. Edits are saved; approving again goes through."""
```
In `save_edits`: record what the reviewer was shown, recompute with the ledger, and block on anything new. Replace
```python
    ext.canonical = normalised
    ext.validation_flags = validate_payload(payload)
    session.flush()

    if approve:
        approve_extraction(session, ext.id, user)
    return edits
```
with
```python
    shown = list(ext.validation_flags or [])
    ext.canonical = normalised
    ext.validation_flags = validate_payload(payload) + check_against_ledger(session, payload)
    session.flush()

    if approve:
        new = [f for f in ext.validation_flags if f not in shown]
        if new:
            raise ApprovalBlocked(
                "Checks changed since you opened this — review the new flags and approve again"
            )
        approve_extraction(session, ext.id, user)
    return edits
```
Note `edit_action` catches `ReviewError` and returns a redirect, so the request's session still commits — the edits and the new flags are saved, as the spec requires.

- [ ] **Step 5: Run to verify**

Run: `uv run pytest tests/ledger tests/review tests/normalize tests/pipeline -q`
Expected: PASS. Existing review tests that approve via the edit form start from `validation_flags=[]`; if one now hits `ApprovalBlocked` because the ledger adds a flag (e.g. a partial against an unknown invoice), make that fixture a full payment or seed the flags it would produce — the block itself is correct behaviour.

- [ ] **Step 6: Commit**
```bash
git add ar_pipeline/normalize/service.py ar_pipeline/review/service.py tests/ledger/test_routing.py
git commit -m "feat(ledger): ledger checks in routing; re-check at approval"
```

---

### Task 7: CSV import of open invoices

**Files:**
- Create: `ar_pipeline/ledger/csv_import.py`
- Test: `tests/ledger/test_csv_import.py`

**Interfaces:**
- Consumes: `Invoice`, `number_key`, `find_invoice`, `format_money`, `TOLERANCE`.
- Produces:
  - `TEMPLATE_CSV: str` (header row + newline)
  - `class CsvImportError(ValueError)` — whole-file problems
  - `@dataclass ImportResult(imported: int, updated: int, skipped: list[str])`
  - `import_open_invoices(session, data: bytes) -> ImportResult`

- [ ] **Step 1: Write the failing tests**

`tests/ledger/test_csv_import.py`:
```python
from decimal import Decimal

import pytest

from ar_pipeline.ledger.csv_import import CsvImportError, import_open_invoices
from ar_pipeline.ledger.posting import find_invoice

HEADER = "invoice_number,payer_name,invoice_date,invoice_amount,currency,outstanding_amount\n"


def _run(db_session, body: str):
    return import_open_invoices(db_session, (HEADER + body).encode())


def test_good_file(db_session):
    r = _run(db_session, 'MST-2026-7550,Orion Fabricators Pvt Ltd,2026-09-01,"1,80,000.00",INR,\n'
                         "MST-2026-7601,,2026-09-05,110000,,60000\n")
    assert (r.imported, r.updated, r.skipped) == (2, 0, [])
    inv = find_invoice(db_session, "MST-2026-7601")
    assert inv.source == "books"
    assert inv.currency == "INR"
    assert inv.paid_before_import == Decimal("50000.00")
    assert find_invoice(db_session, "mst/2026/7550").amount == Decimal("180000.00")


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (",,,100,,", "row 2: invoice_number is blank"),
        ("A-1,,,abc,,", "row 2: invoice_amount is not a number"),
        ("A-1,,,0,,", "row 2: invoice_amount must be more than 0"),
        ("A-1,,,100,,-1", "row 2: outstanding_amount must be between 0 and invoice_amount"),
        ("A-1,,,100,,150", "row 2: outstanding_amount must be between 0 and invoice_amount"),
        ("A-1,,01/09/2026,100,,", "row 2: invoice_date must be YYYY-MM-DD"),
        ("A-1,,,100,RUPEES,", "row 2: currency must be a 3-letter code like INR"),
    ],
)
def test_bad_rows_are_skipped_with_a_numbered_reason(db_session, row, reason):
    r = _run(db_session, row + "\n")
    assert r.imported == 0
    assert r.skipped == [reason]


def test_duplicate_numbers_in_the_file_skip_both_rows(db_session):
    r = _run(db_session, "A-1,,,100,,\na/1,,,200,,\nB-2,,,50,,\n")
    assert r.imported == 1
    assert r.skipped == [
        "row 2: A-1 appears more than once in the file",
        "row 3: a/1 appears more than once in the file",
    ]


def test_unverified_invoice_is_upgraded_and_noted(db_session, make_invoice, post_payment):
    inv = make_invoice("INV-1", "100", source="email")
    post_payment(inv, "25")
    r = _run(db_session, "INV-1,Acme Corp,,120,,\n")
    assert (r.imported, r.updated) == (0, 1)
    db_session.refresh(inv)
    assert (inv.source, inv.amount) == ("books", Decimal("120.00"))
    assert inv.note == "email said ₹100.00, your books say ₹120.00"


def test_missing_required_column(db_session):
    with pytest.raises(CsvImportError, match="missing column"):
        import_open_invoices(db_session, b"invoice_number,payer_name\nA-1,x\n")


def test_too_big(db_session):
    with pytest.raises(CsvImportError, match="1 MB"):
        import_open_invoices(db_session, b"x" * 1_000_001)


def test_too_many_rows(db_session):
    body = "".join(f"A-{i},,,1,,\n" for i in range(5001))
    with pytest.raises(CsvImportError, match="5,000"):
        _run(db_session, body)


def test_not_utf8(db_session):
    with pytest.raises(CsvImportError, match="UTF-8"):
        import_open_invoices(db_session, HEADER.encode() + b"\xff\xfe\n")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/ledger/test_csv_import.py -q`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`ar_pipeline/ledger/csv_import.py`:
```python
"""Import the client's open invoices from a CSV export (spec §3 "CSV import").

The CSV stands in for the client's ERP: its amounts win over anything learned
from emails. Whole-file problems raise ``CsvImportError``; bad rows are skipped
with a reason naming the spreadsheet row number (header = row 1).
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from ar_pipeline.db.models import Invoice
from ar_pipeline.ledger.balance import TOLERANCE
from ar_pipeline.ledger.matching import number_key
from ar_pipeline.ledger.money import format_money
from ar_pipeline.ledger.posting import find_invoice

MAX_BYTES = 1_000_000
MAX_ROWS = 5000
REQUIRED = ("invoice_number", "invoice_amount")
COLUMNS = ("invoice_number", "payer_name", "invoice_date", "invoice_amount", "currency",
           "outstanding_amount")
TEMPLATE_CSV = ",".join(COLUMNS) + "\n"
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class CsvImportError(ValueError):
    pass


@dataclass
class ImportResult:
    imported: int = 0
    updated: int = 0
    skipped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Row:
    number: str
    payer_name: str | None
    invoice_date: date | None
    amount: Decimal
    currency: str
    outstanding: Decimal | None


def _money(raw: str) -> Decimal:
    value = Decimal(raw.replace(",", ""))
    if not value.is_finite():
        raise InvalidOperation
    return value


def _parse(n: int, raw: dict[str, str]) -> _Row | str:
    number = raw.get("invoice_number", "")
    if not number:
        return f"row {n}: invoice_number is blank"
    try:
        amount = _money(raw.get("invoice_amount", ""))
    except InvalidOperation:
        return f"row {n}: invoice_amount is not a number"
    if amount <= 0:
        return f"row {n}: invoice_amount must be more than 0"
    outstanding: Decimal | None = None
    if raw.get("outstanding_amount"):
        try:
            outstanding = _money(raw["outstanding_amount"])
        except InvalidOperation:
            return f"row {n}: outstanding_amount is not a number"
        if outstanding < 0 or outstanding > amount:
            return f"row {n}: outstanding_amount must be between 0 and invoice_amount"
    invoice_date: date | None = None
    if raw.get("invoice_date"):
        try:
            invoice_date = date.fromisoformat(raw["invoice_date"])
        except ValueError:
            return f"row {n}: invoice_date must be YYYY-MM-DD"
    currency = (raw.get("currency") or "INR").upper()
    if not _CURRENCY_RE.match(currency):
        return f"row {n}: currency must be a 3-letter code like INR"
    return _Row(number, raw.get("payer_name") or None, invoice_date, amount, currency, outstanding)


def import_open_invoices(session: Session, data: bytes) -> ImportResult:
    if len(data) > MAX_BYTES:
        raise CsvImportError("the file is larger than 1 MB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvImportError("the file isn't UTF-8 text — save it as CSV (UTF-8)") from None

    reader = csv.DictReader(io.StringIO(text))
    headers = {(h or "").strip().lower() for h in reader.fieldnames or []}
    missing = [c for c in REQUIRED if c not in headers]
    if missing:
        raise CsvImportError(f"missing column(s): {', '.join(missing)}")

    raw_rows: list[tuple[int, dict[str, str]]] = []
    for n, raw in enumerate(reader, start=2):
        if len(raw_rows) >= MAX_ROWS:
            raise CsvImportError("the file has more than 5,000 rows")
        cleaned = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        if not any(cleaned.values()):
            continue  # blank line
        raw_rows.append((n, cleaned))

    result = ImportResult()
    parsed: list[tuple[int, _Row]] = []
    for n, raw in raw_rows:
        row = _parse(n, raw)
        if isinstance(row, str):
            result.skipped.append(row)
        else:
            parsed.append((n, row))

    counts = Counter(number_key(r.number) for _, r in parsed)
    for n, row in parsed:
        if counts[number_key(row.number)] > 1:
            result.skipped.append(f"row {n}: {row.number} appears more than once in the file")
            continue
        paid_before = row.amount - row.outstanding if row.outstanding is not None else Decimal("0")
        existing = find_invoice(session, row.number)
        if existing is None:
            session.add(
                Invoice(
                    invoice_number=row.number,
                    number_key=number_key(row.number),
                    payer_name=row.payer_name,
                    invoice_date=row.invoice_date,
                    amount=row.amount,
                    currency=row.currency,
                    source="books",
                    paid_before_import=paid_before,
                )
            )
            result.imported += 1
            continue
        if existing.source == "email" and abs(existing.amount - row.amount) > TOLERANCE:
            existing.note = (
                f"email said {format_money(existing.amount, existing.currency)}, "
                f"your books say {format_money(row.amount, row.currency)}"
            )
        existing.invoice_number = row.number
        existing.payer_name = row.payer_name or existing.payer_name
        existing.invoice_date = row.invoice_date or existing.invoice_date
        existing.amount = row.amount
        existing.currency = row.currency
        existing.source = "books"
        existing.paid_before_import = paid_before
        result.updated += 1

    result.skipped.sort(key=lambda s: int(s.split()[1].rstrip(":")))
    session.flush()
    return result
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/ledger/test_csv_import.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/ledger/csv_import.py tests/ledger/test_csv_import.py
git commit -m "feat(ledger): import open invoices from CSV"
```

---

### Task 8: Invoices tab, invoice detail page, upload and template

**Files:**
- Create: `ar_pipeline/ledger/queries.py`
- Create: `ar_pipeline/review/templates/invoices.html`, `_invoices_rows.html`, `invoice_detail.html`
- Modify: `ar_pipeline/review/app.py`, `ar_pipeline/review/templates/base.html`, `ar_pipeline/review/static/review.css`
- Test: `tests/ledger/test_queries.py`, `tests/review/test_invoices_tab.py`

**Interfaces:**
- Consumes: Tasks 1–7.
- Produces (in `ar_pipeline.ledger.queries`):
  - `@dataclass(frozen=True) InvoiceRow(id, invoice_number, payer_name, amount, currency, paid, awaiting_review, outstanding, status, source, progress_pct: int, needs_attention: bool)`
  - `list_invoice_rows(session) -> list[InvoiceRow]` (ordered by `invoice_number`)
  - `summarize(rows) -> dict[str, object]` with keys `outstanding_total` (Decimal, INR rows with positive outstanding), `partially_paid`, `paid`, `attention` (ints)
  - `filter_rows(rows, status: str | None) -> list[InvoiceRow]` — `status` is one of `partially_paid`, `paid`, `attention`, `outstanding` (open or partially paid), or None
  - `@dataclass(frozen=True) LedgerEntry(payment_date, reference, amount_paid, deductions, settled, balance_after, extraction_id, email_id, applied: bool)`
  - `@dataclass(frozen=True) AwaitingEntry(extraction_id, subject, settled)`
  - `@dataclass(frozen=True) InvoiceDetail(row: InvoiceRow, note: str | None, paid_before_import: Decimal, entries: list[LedgerEntry], awaiting: list[AwaitingEntry])`
  - `invoice_detail(session, invoice_id: uuid.UUID) -> InvoiceDetail | None`
- Routes (all under `/review`, declared **before** the `/{extraction_id}` routes, right after `approved_rows_fragment`, because `/{extraction_id}` would otherwise capture `invoices` and 422 on the UUID):
  - `GET /invoices`, `GET /invoices-rows`, `POST /invoices/import`, `GET /invoices/template.csv`, `GET /invoices/{invoice_id}` (declare `template.csv` before `{invoice_id}`).
- Jinja filter `money`: `{{ value|money(currency) }}`.

- [ ] **Step 1: Write the failing query tests**

`tests/ledger/test_queries.py`:
```python
from datetime import date
from decimal import Decimal

from ar_pipeline.ledger.queries import filter_rows, invoice_detail, list_invoice_rows, summarize


def test_rows_carry_balance_awaiting_and_progress(db_session, make_invoice, post_payment, make_extraction):
    inv = make_invoice("INV-1", "100")
    post_payment(inv, "25")
    make_extraction(invoice_number="INV-1", invoice_amount="50", amount_paid="50")
    (row,) = list_invoice_rows(db_session)
    assert (row.paid, row.outstanding, row.awaiting_review) == (
        Decimal("25"), Decimal("75"), Decimal("50"),
    )
    assert row.status == "partially_paid"
    assert row.progress_pct == 25
    assert row.needs_attention is False


def test_summary_and_filters(db_session, make_invoice, post_payment):
    a = make_invoice("A-1", "100")
    b = make_invoice("B-1", "100")
    make_invoice("C-1", "100")
    post_payment(a, "40")
    post_payment(b, "130")
    rows = list_invoice_rows(db_session)
    s = summarize(rows)
    assert s["outstanding_total"] == Decimal("160")  # 60 + 100; overpaid B doesn't subtract
    assert (s["partially_paid"], s["paid"], s["attention"]) == (1, 0, 1)
    assert [r.invoice_number for r in filter_rows(rows, "attention")] == ["B-1"]
    assert [r.invoice_number for r in filter_rows(rows, "outstanding")] == ["A-1", "C-1"]


def test_detail_running_balance(db_session, make_invoice, post_payment):
    inv = make_invoice("INV-1", "100", paid_before_import="10")
    for amt, day in (("25", 1), ("50", 4), ("30", 8)):
        post_payment(inv, amt, payment_date=date(2026, 10, day))
    detail = invoice_detail(db_session, inv.id)
    assert detail.paid_before_import == Decimal("10")
    assert [e.balance_after for e in detail.entries] == [Decimal("65"), Decimal("15"), Decimal("-15")]
    assert detail.row.status == "overpaid"
```

- [ ] **Step 2: Write the failing route tests**

`tests/review/test_invoices_tab.py`:
```python
from decimal import Decimal

from ar_pipeline.db.models import Invoice
from ar_pipeline.ledger.matching import number_key


def _invoice(db_session, number="MST-2026-7550", amount="180000", source="books") -> Invoice:
    inv = Invoice(invoice_number=number, number_key=number_key(number), amount=Decimal(amount),
                  source=source, payer_name="Orion Fabricators Pvt Ltd")
    db_session.add(inv)
    db_session.flush()
    return inv


def test_nav_has_invoices(client):
    assert 'href="/review/invoices"' in client.get("/review").text


def test_invoices_page_lists_invoices_with_indian_grouping(client, db_session):
    _invoice(db_session)
    page = client.get("/review/invoices")
    assert page.status_code == 200
    assert "MST-2026-7550" in page.text
    assert "₹1,80,000.00" in page.text
    assert "your books" in page.text
    assert 'data-poll-url="/review/invoices-rows' in page.text


def test_unverified_badge(client, db_session):
    _invoice(db_session, source="email")
    assert "unverified" in client.get("/review/invoices-rows").text


def test_status_filter(client, db_session):
    _invoice(db_session, "A-1")
    text = client.get("/review/invoices?status=paid").text
    assert "A-1" not in text


def test_upload_imports_and_reports_skips(client, db_session):
    csv = b"invoice_number,invoice_amount\nA-1,100\nB-2,abc\n"
    r = client.post("/review/invoices/import", files={"file": ("open.csv", csv, "text/csv")})
    assert r.status_code == 200
    assert "Imported 1" in r.text
    assert "1 row skipped" in r.text
    assert "row 3: invoice_amount is not a number" in r.text


def test_upload_whole_file_error(client):
    r = client.post("/review/invoices/import", files={"file": ("x.csv", b"foo\n1\n", "text/csv")})
    assert r.status_code == 200
    assert "missing column" in r.text


def test_template_download(client):
    r = client.get("/review/invoices/template.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert r.text.startswith("invoice_number,payer_name,invoice_date,invoice_amount")


def test_detail_page(client, db_session):
    inv = _invoice(db_session)
    r = client.get(f"/review/invoices/{inv.id}")
    assert r.status_code == 200
    assert "MST-2026-7550" in r.text


def test_detail_404(client):
    assert client.get("/review/invoices/00000000-0000-0000-0000-000000000000").status_code == 404


def test_requires_login():
    from fastapi.testclient import TestClient

    from ar_pipeline.main import app

    with TestClient(app, follow_redirects=False) as anon:
        assert anon.get("/review/invoices").status_code in (303, 401)
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/ledger/test_queries.py tests/review/test_invoices_tab.py -q`
Expected: FAIL — `ModuleNotFoundError: ar_pipeline.ledger.queries` and 404/422s on the routes.

- [ ] **Step 4: Implement the queries**

`ar_pipeline/ledger/queries.py`:
```python
"""Read models for the Invoices tab, invoice detail, and the review screen's
per-line ledger strip."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Email, Extraction, Invoice, InvoicePayment
from ar_pipeline.ledger.balance import InvoiceBalance, awaiting_by_key, balances, line_settled
from ar_pipeline.ledger.matching import number_key

_ZERO = Decimal("0")


@dataclass(frozen=True)
class InvoiceRow:
    id: uuid.UUID
    invoice_number: str
    payer_name: str | None
    amount: Decimal
    currency: str
    paid: Decimal
    awaiting_review: Decimal
    outstanding: Decimal
    status: str
    source: str
    progress_pct: int
    needs_attention: bool


def _row(bal: InvoiceBalance, awaiting: dict[str, Decimal]) -> InvoiceRow:
    inv = bal.invoice
    amount = Decimal(inv.amount)
    pct = int(min(Decimal(100), max(_ZERO, bal.paid / amount * 100))) if amount > 0 else 0
    return InvoiceRow(
        id=inv.id,
        invoice_number=inv.invoice_number,
        payer_name=inv.payer_name,
        amount=amount,
        currency=inv.currency,
        paid=bal.paid,
        awaiting_review=awaiting.get(inv.number_key, _ZERO),
        outstanding=bal.outstanding,
        status=bal.status,
        source=inv.source,
        progress_pct=pct,
        needs_attention=bal.status == "overpaid" or bool(inv.note),
    )


def list_invoice_rows(session: Session) -> list[InvoiceRow]:
    invoices = list(session.scalars(select(Invoice).order_by(Invoice.invoice_number)))
    awaiting = awaiting_by_key(session)
    return [_row(b, awaiting) for b in balances(session, invoices)]


def summarize(rows: list[InvoiceRow]) -> dict[str, object]:
    return {
        "outstanding_total": sum(
            (r.outstanding for r in rows if r.currency == "INR" and r.outstanding > 0), _ZERO
        ),
        "partially_paid": sum(1 for r in rows if r.status == "partially_paid"),
        "paid": sum(1 for r in rows if r.status == "paid"),
        "attention": sum(1 for r in rows if r.needs_attention),
    }


def filter_rows(rows: list[InvoiceRow], status: str | None) -> list[InvoiceRow]:
    if status == "attention":
        return [r for r in rows if r.needs_attention]
    if status == "outstanding":
        return [r for r in rows if r.status in ("open", "partially_paid")]
    if status in ("partially_paid", "paid"):
        return [r for r in rows if r.status == status]
    return rows


@dataclass(frozen=True)
class LedgerEntry:
    payment_date: date | None
    reference: str | None
    amount_paid: Decimal
    deductions: Decimal
    settled: Decimal
    balance_after: Decimal
    extraction_id: uuid.UUID
    email_id: uuid.UUID
    applied: bool  # False for a different-currency payment (not counted)


@dataclass(frozen=True)
class AwaitingEntry:
    extraction_id: uuid.UUID
    subject: str
    settled: Decimal


@dataclass(frozen=True)
class InvoiceDetail:
    row: InvoiceRow
    note: str | None
    paid_before_import: Decimal
    entries: list[LedgerEntry]
    awaiting: list[AwaitingEntry]


def invoice_detail(session: Session, invoice_id: uuid.UUID) -> InvoiceDetail | None:
    inv = session.get(Invoice, invoice_id)
    if inv is None:
        return None
    row = _row(balances(session, [inv])[0], awaiting_by_key(session))
    balance = Decimal(inv.amount) - Decimal(inv.paid_before_import or 0)
    entries: list[LedgerEntry] = []
    payments = session.execute(
        select(InvoicePayment, Extraction.email_id)
        .join(Extraction, Extraction.id == InvoicePayment.extraction_id)
        .where(InvoicePayment.invoice_id == inv.id)
        .order_by(
            InvoicePayment.payment_date.asc().nulls_last(),
            InvoicePayment.created_at.asc(),
            InvoicePayment.id.asc(),
        )
    )
    for pay, email_id in payments:
        applied = pay.currency == inv.currency
        if applied:
            balance -= pay.settled
        entries.append(
            LedgerEntry(pay.payment_date, pay.payment_reference, pay.amount_paid,
                        pay.deductions_total, pay.settled, balance, pay.extraction_id,
                        email_id, applied)
        )
    awaiting: list[AwaitingEntry] = []
    pending = session.execute(
        select(Extraction, Email.subject)
        .join(Email, Email.id == Extraction.email_id)
        .where(Extraction.status == "pending_review", Extraction.is_remittance.is_(True))
    )
    for ext, subject in pending:
        for line in (ext.canonical or {}).get("line_items") or []:
            if number_key(str(line.get("invoice_number") or "")) == inv.number_key:
                awaiting.append(AwaitingEntry(ext.id, subject, line_settled(line)))
    return InvoiceDetail(row, inv.note, Decimal(inv.paid_before_import or 0), entries, awaiting)
```
Note: entries are ordered by `payment_date` first because `created_at` is `now()` — identical for every row written in one transaction — and `id` is a random uuid4. That's why `test_detail_running_balance` passes distinct `payment_date`s.

- [ ] **Step 5: Add routes and the money filter**

In `ar_pipeline/review/app.py`:
- Imports: `from decimal import Decimal`, and add `File, UploadFile` to the `fastapi` import.
- After `templates = Jinja2Templates(...)`:
```python
def _money_filter(value: object, currency: str = "INR") -> str:
    from ar_pipeline.ledger.money import format_money

    return format_money(Decimal(str(value)), currency)


templates.env.filters["money"] = _money_filter
```
- Right after `approved_rows_fragment`, add:
```python
def _invoices_ctx(session: Session, status: str | None) -> dict[str, object]:
    from ar_pipeline.ledger.queries import filter_rows, list_invoice_rows, summarize

    rows = list_invoice_rows(session)
    return {"rows": filter_rows(rows, status), "summary": summarize(rows), "status": status}


@router.get("/invoices", response_class=HTMLResponse)
def invoices_page(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    status = request.query_params.get("status")
    return _render(
        request, "invoices.html", user=user, result=None, import_error=None,
        flash=request.query_params.get("flash"), **_invoices_ctx(session, status),
    )


@router.get("/invoices-rows", response_class=HTMLResponse)
def invoices_rows_fragment(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    status = request.query_params.get("status")
    return _render(request, "_invoices_rows.html", **_invoices_ctx(session, status))


@router.post("/invoices/import", response_class=HTMLResponse)
async def invoices_import(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.ledger.csv_import import MAX_BYTES, CsvImportError, import_open_invoices

    data = await file.read(MAX_BYTES + 1)
    result = error = None
    try:
        result = import_open_invoices(session, data)
    except CsvImportError as exc:
        error = str(exc)
    # rendered, not redirected: the skipped-row list must be shown on the page
    return _render(
        request, "invoices.html", user=user, result=result, import_error=error, flash=None,
        **_invoices_ctx(session, None),
    )


@router.get("/invoices/template.csv")
def invoices_template(user: User = Depends(require_user)) -> Response:
    from ar_pipeline.ledger.csv_import import TEMPLATE_CSV

    return Response(
        TEMPLATE_CSV,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="open-invoices-template.csv"'},
    )


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail_page(
    request: Request,
    invoice_id: uuid.UUID,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.ledger.queries import invoice_detail

    detail = invoice_detail(session, invoice_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="not found")
    return _render(request, "invoice_detail.html", user=user, d=detail, flash=None)
```
(Import `MAX_BYTES` as shown; the 1 MB check itself lives in `import_open_invoices` — reading `MAX_BYTES + 1` bytes lets it see the file is too big without reading an unbounded upload.)

- [ ] **Step 6: Templates**

`ar_pipeline/review/templates/base.html` — add after the Approved link:
```html
      <a href="/review/invoices" class="{{ 'active' if path.startswith('/review/invoices') else '' }}">Invoices</a>
```

`ar_pipeline/review/templates/invoices.html`:
```html
{% extends "base.html" %}
{% block title %}Invoices — AR Review by MLDeep Systems{% endblock %}
{% block content %}
<div class="masthead-row">
  <h1>Invoices <small>— what's been paid against each invoice</small></h1>
</div>
<p class="lede">Balances count <strong>approved</strong> payments only. Amounts marked
  <span class="badge warn">unverified</span> were learned from a payer's email; upload your open
  invoices to replace them with the figures from your books.</p>

<form method="post" action="/review/invoices/import" enctype="multipart/form-data" class="upload-bar">
  <label for="invoiceCsv">Open invoices (CSV)</label>
  <input type="file" id="invoiceCsv" name="file" accept=".csv,text/csv" required>
  <button type="submit">Import open invoices</button>
  <a href="/review/invoices/template.csv">Download a template</a>
</form>

{% if import_error %}<p class="import-result bad" role="alert">Import failed: {{ import_error }}</p>{% endif %}
{% if result %}
<div class="import-result" role="status">
  <p><strong>Imported {{ result.imported }}</strong> &middot; updated {{ result.updated }} &middot;
    {{ result.skipped|length }} row{{ "" if result.skipped|length == 1 else "s" }} skipped</p>
  {% if result.skipped %}<ul>{% for s in result.skipped %}<li>{{ s }}</li>{% endfor %}</ul>{% endif %}
</div>
{% endif %}

<div id="invoices-live" data-poll-url="/review/invoices-rows{% if status %}?status={{ status }}{% endif %}" data-poll-interval="3500">
  {% include "_invoices_rows.html" %}
</div>
{% endblock %}
```

`ar_pipeline/review/templates/_invoices_rows.html`:
```html
<div class="stat-row">
  <a class="stat-tile{% if status == 'outstanding' %} active{% endif %}" href="/review/invoices?status=outstanding">
    <div class="n">{{ summary.outstanding_total|money }}</div><div class="label">Outstanding (INR)</div>
  </a>
  <a class="stat-tile{% if status == 'partially_paid' %} active{% endif %}" href="/review/invoices?status=partially_paid">
    <div class="n warn">{{ summary.partially_paid }}</div><div class="label">Partially paid</div>
  </a>
  <a class="stat-tile{% if status == 'paid' %} active{% endif %}" href="/review/invoices?status=paid">
    <div class="n good">{{ summary.paid }}</div><div class="label">Paid</div>
  </a>
  <a class="stat-tile{% if status == 'attention' %} active{% endif %}" href="/review/invoices?status=attention">
    <div class="n warn">{{ summary.attention }}</div><div class="label">Needs attention</div>
  </a>
</div>
{% if status %}<p class="lede">Filtered. <a href="/review/invoices">Show all invoices</a></p>{% endif %}
{% if not rows %}
<p>No invoices yet. Upload your open invoices, or approve a payment to start the ledger.</p>
{% else %}
<div class="table-wrap">
<table>
  <thead><tr>
    <th>Invoice</th><th>Payer</th><th class="num">Amount</th><th class="num">Paid</th>
    <th class="num">Awaiting review</th><th class="num">Outstanding</th><th>Progress</th>
    <th>Status</th><th>Source</th>
  </tr></thead>
  <tbody>
  {% for r in rows %}
  <tr>
    <td><a href="/review/invoices/{{ r.id }}">{{ r.invoice_number }}</a></td>
    <td class="truncate" title="{{ r.payer_name or '' }}">{{ r.payer_name or "—" }}</td>
    <td class="num">{{ r.amount|money(r.currency) }}</td>
    <td class="num">{{ r.paid|money(r.currency) }}</td>
    <td class="num">{% if r.awaiting_review %}{{ r.awaiting_review|money(r.currency) }}{% else %}—{% endif %}</td>
    <td class="num">{{ r.outstanding|money(r.currency) }}</td>
    <td><div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100"
             aria-valuenow="{{ r.progress_pct }}" aria-label="{{ r.progress_pct }}% paid">
          <span style="width: {{ r.progress_pct }}%"></span></div></td>
    <td>{% if r.status == "paid" %}<span class="badge ok">paid</span>
        {% elif r.status == "overpaid" %}<span class="badge bad">overpaid</span>
        {% elif r.status == "partially_paid" %}<span class="badge warn">partially paid</span>
        {% else %}<span class="badge">open</span>{% endif %}</td>
    <td>{% if r.source == "books" %}<span class="badge">your books</span>
        {% else %}<span class="badge warn">unverified</span>{% endif %}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
</div>
{% endif %}
```

`ar_pipeline/review/templates/invoice_detail.html`:
```html
{% extends "base.html" %}
{% set r = d.row %}
{% block title %}{{ r.invoice_number }} — AR Review by MLDeep Systems{% endblock %}
{% block content %}
<p class="lede"><a href="/review/invoices">&larr; All invoices</a></p>
<h1>{{ r.invoice_number }}
  {% if r.source == "books" %}<span class="badge">your books</span>{% else %}<span class="badge warn">unverified</span>{% endif %}
</h1>
<p class="meta">{{ r.payer_name or "Payer not recorded" }} &middot; invoice {{ r.amount|money(r.currency) }}
  &middot; paid {{ r.paid|money(r.currency) }} &middot; outstanding <strong>{{ r.outstanding|money(r.currency) }}</strong></p>
<div class="progress progress-lg" role="progressbar" aria-valuemin="0" aria-valuemax="100"
     aria-valuenow="{{ r.progress_pct }}" aria-label="{{ r.progress_pct }}% paid"><span style="width: {{ r.progress_pct }}%"></span></div>
{% if r.status == "overpaid" %}<p role="alert"><span class="badge bad">overpaid by {{ (0 - r.outstanding)|money(r.currency) }}</span></p>{% endif %}
{% if d.note %}<p class="field-flag">{{ d.note }}</p>{% endif %}

<h2>Payment history</h2>
<div class="table-wrap">
<table>
  <thead><tr><th>Date</th><th>Reference</th><th class="num">Paid</th><th class="num">Deductions</th>
    <th class="num">Settled</th><th class="num">Balance after</th><th></th></tr></thead>
  <tbody>
  {% if d.paid_before_import %}
  <tr><td>—</td><td>Paid before import</td><td class="num">—</td><td class="num">—</td>
    <td class="num">{{ d.paid_before_import|money(r.currency) }}</td>
    <td class="num">{{ (r.amount - d.paid_before_import)|money(r.currency) }}</td><td></td></tr>
  {% endif %}
  {% for e in d.entries %}
  <tr>
    <td>{{ e.payment_date.strftime("%d %b %Y") if e.payment_date else "—" }}</td>
    <td>{{ e.reference or "—" }}</td>
    <td class="num">{{ e.amount_paid|money(r.currency) }}</td>
    <td class="num">{{ e.deductions|money(r.currency) }}</td>
    <td class="num">{{ e.settled|money(r.currency) }}{% if not e.applied %} <span class="badge warn">other currency — not counted</span>{% endif %}</td>
    <td class="num">{% if e.balance_after < 0 %}<strong>{{ e.balance_after|money(r.currency) }} overpaid</strong>{% else %}{{ e.balance_after|money(r.currency) }}{% endif %}</td>
    <td><a href="/review/email/{{ e.email_id }}">Email</a> &middot; <a href="/review/extraction/{{ e.extraction_id }}">JSON</a></td>
  </tr>
  {% else %}
  <tr><td colspan="7">No approved payments yet.</td></tr>
  {% endfor %}
  </tbody>
</table>
</div>

{% if d.awaiting %}
<h2>Awaiting review <small>— not counted in the balance</small></h2>
<ul>
  {% for a in d.awaiting %}
  <li><a href="/review/{{ a.extraction_id }}">{{ a.subject }}</a> — {{ a.settled|money(r.currency) }}</li>
  {% endfor %}
</ul>
{% endif %}
{% endblock %}
```

- [ ] **Step 7: CSS**

Append to `ar_pipeline/review/static/review.css`:
```css
/* Invoices tab */
.upload-bar { display: flex; flex-wrap: wrap; align-items: center; gap: .6rem 1rem; margin: 0 0 1rem; }
.upload-bar label { font-weight: 600; font-size: .9rem; }
.import-result { border: 1px solid var(--border); background: var(--surface); border-radius: var(--radius); padding: .6rem .9rem; margin: 0 0 1rem; }
.import-result.bad { border-color: var(--bad); color: var(--bad); }
.import-result ul { margin: .3rem 0 0; padding-left: 1.2rem; font-size: .85rem; color: var(--text-muted); }
.progress { width: 7rem; height: .5rem; border-radius: 999px; background: var(--surface-muted); border: 1px solid var(--border); overflow: hidden; }
.progress > span { display: block; height: 100%; background: var(--accent-fill); }
.progress-lg { width: 100%; max-width: 32rem; height: .75rem; margin: .4rem 0 1rem; }
.stat-tile.active { border-color: var(--brand); }
```
Check `--bad` exists in `:root` (`grep -n "\-\-bad" ar_pipeline/review/static/review.css`); if the token is named differently, use that name.

- [ ] **Step 8: Run to verify**

Run: `uv run pytest tests/ledger/test_queries.py tests/review/test_invoices_tab.py -q`
Expected: PASS. Then `uv run pytest tests/review -q` (nav change touches every page).

- [ ] **Step 9: Commit**
```bash
git add ar_pipeline/ledger/queries.py ar_pipeline/review tests/ledger/test_queries.py tests/ledger/conftest.py tests/review/test_invoices_tab.py
git commit -m "feat(review): Invoices tab, invoice detail, CSV upload"
```

---

### Task 9: Ledger strip and "Use INV-…" on the review screen

**Files:**
- Modify: `ar_pipeline/ledger/queries.py` (add `LineLedger`, `line_ledgers`)
- Modify: `ar_pipeline/review/service.py` (add `use_invoice`)
- Modify: `ar_pipeline/review/app.py` (`detail_page` passes `line_ledgers`; new `POST /{extraction_id}/use-invoice`)
- Modify: `ar_pipeline/review/templates/detail.html`, `ar_pipeline/review/static/review.css`
- Test: `tests/review/test_ledger_strip.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) LineLedger(invoice_number: str, currency: str, invoice_id: uuid.UUID | None, source: str | None, amount: Decimal | None, paid: Decimal | None, outstanding: Decimal | None, after_this: Decimal | None, awaiting_elsewhere: Decimal, suggestions: list[tuple[uuid.UUID, str]])`
  - `line_ledgers(session, extraction: Extraction) -> list[LineLedger]` — one per canonical line, same order
  - `use_invoice(session, extraction_id: uuid.UUID, user: User, line_index: int, invoice_id: uuid.UUID) -> str` — returns the invoice number now on the line; raises `ReviewError`

- [ ] **Step 1: Write the failing tests**

`tests/review/test_ledger_strip.py`:
```python
from decimal import Decimal
from urllib.parse import unquote

from sqlalchemy import select

from ar_pipeline.db.models import ExtractionEdit, Invoice
from ar_pipeline.ledger.matching import number_key


def _invoice(db_session, number, amount="100.00"):
    inv = Invoice(invoice_number=number, number_key=number_key(number), amount=Decimal(amount),
                  source="books", payer_name="Acme Corp")
    db_session.add(inv)
    db_session.flush()
    return inv


def test_strip_shows_balance_and_after_this(client, db_session, seed_pending):
    _invoice(db_session, "INV-1", "200.00")
    _e, ext = seed_pending()  # line: invoice INV-1, settles 100 (90 paid + 10 TDS)
    text = client.get(f"/review/{ext.id}").text
    assert "your books ₹200.00" in text
    assert "outstanding ₹200.00" in text
    assert "after this payment ₹100.00" in text


def test_near_match_offers_use_button_and_rewrites_the_line(client, db_session, seed_pending):
    inv = _invoice(db_session, "INV-2026-1")
    _e, ext = seed_pending()
    ext.canonical["line_items"] = [{**ext.canonical["line_items"][0], "invoice_number": "INV-2026-01"}]
    db_session.flush()
    page = client.get(f"/review/{ext.id}").text
    assert "Use INV-2026-1" in page

    r = client.post(f"/review/{ext.id}/use-invoice", data={"line_index": "0", "invoice_id": str(inv.id)})
    assert r.status_code == 303
    assert "Using INV-2026-1" in unquote(r.headers["location"])
    db_session.refresh(ext)
    assert ext.canonical["line_items"][0]["invoice_number"] == "INV-2026-1"
    edits = db_session.scalars(select(ExtractionEdit).where(ExtractionEdit.extraction_id == ext.id)).all()
    assert [e.field_path for e in edits] == ["line_items[0].invoice_number"]
    assert not any("did you mean" in f for f in ext.validation_flags)


def test_use_invoice_rejects_a_bad_line_index(client, db_session, seed_pending):
    inv = _invoice(db_session, "INV-2026-1")
    _e, ext = seed_pending()
    r = client.post(f"/review/{ext.id}/use-invoice", data={"line_index": "5", "invoice_id": str(inv.id)})
    assert r.status_code == 303
    assert "no such line" in unquote(r.headers["location"])
```
Before writing Step 3, check the exact `field_path` string `canonical_diff` produces for a line field (`grep -n "def canonical_diff" -A30 ar_pipeline/review/*.py`) and adjust the asserted path if it differs.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/review/test_ledger_strip.py -q`
Expected: FAIL — strip text missing; `use-invoice` route 404/405.

- [ ] **Step 3: Implement `line_ledgers`**

Append to `ar_pipeline/ledger/queries.py`:
```python
@dataclass(frozen=True)
class LineLedger:
    invoice_number: str
    currency: str
    invoice_id: uuid.UUID | None
    source: str | None
    amount: Decimal | None
    paid: Decimal | None
    outstanding: Decimal | None
    after_this: Decimal | None
    awaiting_elsewhere: Decimal
    suggestions: list[tuple[uuid.UUID, str]]


def line_ledgers(session: Session, extraction: Extraction) -> list[LineLedger]:
    from ar_pipeline.ledger.matching import near_matches

    canonical = extraction.canonical or {}
    header = canonical.get("header") or {}
    currency = str(header.get("currency") or "INR")
    awaiting = awaiting_by_key(session, exclude=extraction.id)
    every_invoice: list[Invoice] | None = None
    out: list[LineLedger] = []
    for line in canonical.get("line_items") or []:
        number = str(line.get("invoice_number") or "")
        key = number_key(number)
        invoice = session.scalar(select(Invoice).where(Invoice.number_key == key)) if key else None
        if invoice is None:
            suggestions: list[tuple[uuid.UUID, str]] = []
            if key:
                if every_invoice is None:
                    every_invoice = list(session.scalars(select(Invoice)))
                suggestions = [
                    (inv.id, inv.invoice_number)
                    for inv in near_matches(number, every_invoice, header.get("payer_name"))
                ]
            out.append(LineLedger(number, currency, None, None, None, None, None, None,
                                  awaiting.get(key, _ZERO), suggestions))
            continue
        bal = balances(session, [invoice])[0]
        out.append(
            LineLedger(
                invoice.invoice_number, invoice.currency, invoice.id, invoice.source,
                Decimal(invoice.amount), bal.paid, bal.outstanding,
                bal.outstanding - line_settled(line), awaiting.get(key, _ZERO), [],
            )
        )
    return out
```

- [ ] **Step 4: Implement `use_invoice`**

In `ar_pipeline/review/service.py` (after `save_edits`):
```python
def use_invoice(
    session: Session, extraction_id: uuid.UUID, user: User, line_index: int, invoice_id: uuid.UUID
) -> str:
    """Reviewer picked a suggested invoice: rewrite that line's number through
    the normal edit path so it's audited and the checks re-run."""
    from ar_pipeline.db.models import Invoice

    ext = _require_pending(session.get(Extraction, extraction_id))
    invoice = session.get(Invoice, invoice_id)
    if invoice is None:
        raise ReviewError("that invoice no longer exists")
    canonical = dict(ext.canonical) if isinstance(ext.canonical, dict) else {}
    lines = [dict(li) for li in canonical.get("line_items") or []]
    if not 0 <= line_index < len(lines):
        raise ReviewError("no such line on this payment")
    lines[line_index]["invoice_number"] = invoice.invoice_number
    save_edits(
        session, extraction_id, user,
        {"header": canonical.get("header", {}), "line_items": lines}, approve=False,
    )
    return invoice.invoice_number
```

- [ ] **Step 5: Route + detail page wiring**

In `ar_pipeline/review/app.py`, add before `@router.post("/{extraction_id}/reprocess")`:
```python
@router.post("/{extraction_id}/use-invoice")
def use_invoice_action(
    extraction_id: uuid.UUID,
    line_index: int = Form(...),
    invoice_id: uuid.UUID = Form(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.review.service import ReviewError, use_invoice

    try:
        number = use_invoice(session, extraction_id, user, line_index, invoice_id)
    except ReviewError as exc:
        return RedirectResponse(f"/review/{extraction_id}?flash={quote(str(exc))}", status_code=303)
    return RedirectResponse(
        f"/review/{extraction_id}?flash={quote(f'Using {number}')}", status_code=303
    )
```
In `detail_page`, import `from ar_pipeline.ledger.queries import line_ledgers` and pass `line_ledgers=line_ledgers(session, view.extraction)` to `_render`.

- [ ] **Step 6: Strip markup**

In `ar_pipeline/review/templates/detail.html`, inside the line-item `<fieldset>`, directly after the `{% for lf in this_line_flags %}…{% endfor %}` line, add:
```html
          {% set led = line_ledgers[li_idx] if line_ledgers and li_idx < line_ledgers|length else none %}
          {% if led %}
          <div class="ledger-strip">
            {% if led.invoice_id %}
              <a href="/review/invoices/{{ led.invoice_id }}">{{ led.invoice_number }}</a>
              &middot; {{ "your books" if led.source == "books" else "unverified" }} {{ led.amount|money(led.currency) }}
              &middot; paid {{ led.paid|money(led.currency) }}
              &middot; outstanding {{ led.outstanding|money(led.currency) }}
              &middot; <strong>after this payment {{ led.after_this|money(led.currency) }}</strong>
            {% elif led.suggestions %}
              Not in the ledger. Did you mean:
              {% for inv_id, inv_number in led.suggestions %}
              <button type="submit" form="useInvoice{{ li_idx }}_{{ loop.index0 }}" class="btn-quiet">Use {{ inv_number }}</button>
              {% endfor %}
            {% else %}
              New invoice — not in the ledger yet.
            {% endif %}
            {% if led.awaiting_elsewhere %}<span class="ledger-note">{{ led.awaiting_elsewhere|money(led.currency) }} more awaiting review on this invoice</span>{% endif %}
          </div>
          {% endif %}
```
The "Use" buttons submit separate forms (a form can't nest inside `#editform`). Add these forms after the closing `</form>` of `#editform`:
```html
    {% for led in line_ledgers or [] %}{% set li_idx = loop.index0 %}
      {% for inv_id, inv_number in led.suggestions %}
      <form method="post" action="/review/{{ view.extraction.id }}/use-invoice" id="useInvoice{{ li_idx }}_{{ loop.index0 }}" hidden>
        <input type="hidden" name="line_index" value="{{ li_idx }}">
        <input type="hidden" name="invoice_id" value="{{ inv_id }}">
      </form>
      {% endfor %}
    {% endfor %}
```
CSS (append to `review.css`):
```css
.ledger-strip { font-size: .85rem; color: var(--text-muted); background: var(--surface-muted); border-radius: var(--radius); padding: .45rem .7rem; margin: .2rem 0 .6rem; display: flex; flex-wrap: wrap; gap: .3rem .5rem; align-items: center; }
.ledger-strip .ledger-note { flex-basis: 100%; color: var(--warn); font-weight: 600; }
```

- [ ] **Step 7: Run to verify**

Run: `uv run pytest tests/review -q`
Expected: PASS.

- [ ] **Step 8: Commit**
```bash
git add ar_pipeline/ledger/queries.py ar_pipeline/review tests/review/test_ledger_strip.py
git commit -m "feat(review): per-line ledger strip and use-invoice suggestion"
```

---

### Task 10: `ar-pipeline ledger-backfill`

**Files:**
- Modify: `ar_pipeline/cli.py`
- Test: `tests/test_cli.py` (add one test)

**Interfaces:**
- Consumes: `backfill(session) -> tuple[int, int]` (Task 4).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:
```python
def test_ledger_backfill_command(wired, capsys, monkeypatch):
    monkeypatch.setattr("ar_pipeline.ledger.posting.backfill", lambda session: (2, 3))
    from ar_pipeline.cli import main

    assert main(["ledger-backfill"]) == 0
    assert "3 payment line(s) posted from 2 approved extraction(s)" in capsys.readouterr().out
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_ledger_backfill_command -q`
Expected: FAIL — argparse `invalid choice: 'ledger-backfill'` (SystemExit 2).

- [ ] **Step 3: Implement**

In `ar_pipeline/cli.py`: add to the module docstring's subcommand list
`* ``ledger-backfill`` — post every already-approved payment into the invoice ledger (safe to re-run).`
Add:
```python
def _cmd_ledger_backfill() -> int:
    from ar_pipeline.db.base import get_session
    from ar_pipeline.ledger import posting

    with get_session() as session:
        extractions, rows = posting.backfill(session)
    print(f"{rows} payment line(s) posted from {extractions} approved extraction(s)")
    return 0
```
In `build_parser` add `sub.add_parser("ledger-backfill", help="post already-approved payments into the invoice ledger")`, and in `main` add `if args.command == "ledger-backfill": return _cmd_ledger_backfill()` before the final return. (`posting.backfill` is looked up at call time so the monkeypatch applies.)

- [ ] **Step 4: Run to verify**

Run: `uv run pytest tests/test_cli.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/cli.py tests/test_cli.py
git commit -m "feat(cli): ledger-backfill"
```

---

### Task 11: Demo data, end-to-end story, final verification

**Files:**
- Create: `demo/open-invoices.csv`, `demo/open-invoices-template.csv`
- Modify: `~/Downloads/demo-emails-round-2.md` (append installment + near-match emails), `~/Downloads/demo-walkthrough.md` (add an Invoices section)
- Test: `tests/ledger/test_end_to_end.py`

- [ ] **Step 1: Write the failing end-to-end test**

`tests/ledger/test_end_to_end.py`:
```python
"""The installment story from the spec: books invoice ₹1,00,000, paid 25k / 50k / 30k
through the real pipeline (fake LLM), ending overpaid by ₹5,000."""

from decimal import Decimal

from sqlalchemy import select

import ar_pipeline.config as config_module
from ar_pipeline.db.models import Extraction
from ar_pipeline.ledger.csv_import import import_open_invoices
from ar_pipeline.ledger.posting import find_invoice
from ar_pipeline.ledger.queries import invoice_detail
from ar_pipeline.normalize.service import normalize_one
from tests.ledger.test_routing import _extracted_email, _output
from tests.normalize.llm_fake import FakeLLMClient


def test_installments_end_overpaid(db_session, monkeypatch):
    monkeypatch.setenv("AUTO_APPROVE_MIN_CONFIDENCE", "0.75")
    config_module.get_settings.cache_clear()
    try:
        import_open_invoices(db_session, b"invoice_number,payer_name,invoice_amount\nINV-1,Acme Corp,100000\n")
        statuses = []
        for i, amount in enumerate(("25000", "50000", "30000")):
            email = _extracted_email(db_session, f"m-e2e-{i}")
            normalize_one(db_session, email, FakeLLMClient(response=_output(amount)))
            ext = db_session.scalars(select(Extraction).where(Extraction.email_id == email.id)).one()
            statuses.append(ext.status)
        # 25k and 50k are clean installments; 30k overpays and is held for review
        assert statuses == ["approved", "approved", "pending_review"]
        flags = ext.validation_flags
        assert any("overpaid by ₹5,000.00" in f for f in flags)

        # reviewer approves it anyway, having seen the flag
        from ar_pipeline.review.auth import User
        from ar_pipeline.review.service import save_edits

        form = {"header": ext.canonical["header"], "line_items": ext.canonical["line_items"]}
        save_edits(db_session, ext.id, User(name="Asha"), form, approve=True)

        inv = find_invoice(db_session, "INV-1")
        detail = invoice_detail(db_session, inv.id)
        assert detail.row.status == "overpaid"
        assert detail.row.outstanding == Decimal("-5000")
    finally:
        config_module.get_settings.cache_clear()
```
Run: `uv run pytest tests/ledger/test_end_to_end.py -q` — expected PASS if Tasks 1–10 are correct (this test exercises them together; a failure here points to an integration bug, not missing code).

- [ ] **Step 2: Demo CSVs**

`demo/open-invoices-template.csv`:
```
invoice_number,payer_name,invoice_date,invoice_amount,currency,outstanding_amount
```
`demo/open-invoices.csv` (amounts chosen to match the demo emails; `MST-2026-7733` shows "paid before import"; `ORB-2026-3390` deliberately absent; `MST-2026-7900` is the installment invoice):
```
invoice_number,payer_name,invoice_date,invoice_amount,currency,outstanding_amount
MST-2026-7550,Orion Fabricators Pvt Ltd,2026-09-01,180000.00,INR,
MST-2026-7601,Orion Fabricators Pvt Ltd,2026-09-05,110000.00,INR,
MST-2026-7602,Orion Fabricators Pvt Ltd,2026-09-05,95400.00,INR,
MST-2026-7712,Deccan Alloys Pvt Ltd,2026-09-10,48500.00,INR,
MST-2026-7733,Kaveri Engineering Pvt Ltd,2026-09-12,100000.00,INR,72000.00
MST-2026-7741,Meridian Steel Traders,2026-09-14,385000.00,INR,
MST-2026-7801,Meridian Steel Traders,2026-09-18,245000.00,INR,
MST-2026-7900,Sahyadri Castings Pvt Ltd,2026-09-20,100000.00,INR,
```
Before finalizing, open `~/Downloads/demo-emails-round-2.md` and `~/Downloads/demo-walkthrough.md` and make each amount above match what its email pays (e.g. the MST-2026-7712 email pays 48,500.00, so the invoice is 48,500.00 and it settles in full; MST-2026-7733 email pays 72,000.00 against 72,000.00 outstanding). Check each against the stub with the dry-run snippet from the walkthrough preparation (`StubLLMClient().parse(...)` + `normalize_email`) so the demo behaves as written.

- [ ] **Step 3: Demo emails**

Append to `~/Downloads/demo-emails-round-2.md` a "Round 3 — invoice ledger" section with four emails, each one payment in the stub-friendly shape (amount + `Invoice <number>` + `UTR:` line + payer sign-off), from Sahyadri Castings Pvt Ltd:
1. `Payment of INR 25,000.00 against Invoice MST-2026-7900` — UTR `SBIN52026100100101` → auto-approves; invoice 25% paid.
2. Same invoice, INR 50,000.00, UTR `SBIN52026100400102` → auto-approves; 75% paid.
3. Same invoice, INR 30,000.00, UTR `SBIN52026100800103` → held: "overpaid by ₹5,000.00".
4. From Meridian Steel Traders: INR 2,45,000.00 against `Invoice MST/2026/780`, UTR `HDFC52026100900104` → held: "did you mean MST-2026-7801?"; clicking **Use MST-2026-7801** clears it.
Dry-run each through the stub before writing its "Expect" line.

- [ ] **Step 4: Walkthrough section**

Append a "7. Invoices tab" section to `~/Downloads/demo-walkthrough.md`:
1. Open **Invoices** — empty or only *unverified* rows from earlier approvals. (If you had approved payments before this change, first run `uv run ar-pipeline ledger-backfill`.)
2. **Download a template**, then upload `demo/open-invoices.csv` → "Imported 8 · updated N · 0 rows skipped".
3. Send Round 3 emails 1–3 one at a time, Poll now after each; watch MST-2026-7900's progress bar move 25% → 75% → overpaid.
4. Open MST-2026-7900 → payment history with balance after each installment, ending "−₹5,000.00 overpaid".
5. Send email 4 → open it in Queue → ledger strip shows "Use MST-2026-7801" → click → flag clears → approve.
Plus the matching checklist rows.

- [ ] **Step 5: Full verification**

Run, in order:
- `uv run ruff check ar_pipeline tests && uv run ruff format --check ar_pipeline tests` → clean (run `uv run ruff format ar_pipeline tests` if formatting differs, then re-check).
- `uv run mypy ar_pipeline` → `Success`.
- `uv run pytest tests/ledger tests/review tests/normalize tests/pipeline tests/test_cli.py tests/db -q` → PASS.
- Full suite in the background: `uv run pytest -q 2>&1 | tail -5` → all pass.
- Dev DB: `uv run python scripts/dev_db.py migrate`, then `uv run ar-pipeline ledger-backfill` → prints the posted count.
- Restart the review app (Python changed) and open http://localhost:8000/review/invoices.

- [ ] **Step 6: Commit**
```bash
git add demo tests/ledger/test_end_to_end.py
git commit -m "feat(ledger): demo invoices and end-to-end installment test"
```
