# Email Threads (Stage 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split forwarded/replied email threads into their messages, process only messages (and payments) not already recorded, never auto-send historical payments, and let reviewers mark history "already recorded".

**Architecture:** A pure splitter (`ar_pipeline/threads/splitter.py`) cuts each email body into `email_message` rows at classify time. Thread memory (`threads/memory.py`) marks a message `seen` when an identical earlier copy, or every bank reference it mentions, is already recorded — those never reach the AI. Each remaining message is normalized on its own (one AI call per message, oldest first, failures isolated per message). After the AI, every payment gets a payment key (`threads/references.py`, `threads/dedupe.py`) enforced by a partial unique index; duplicates are stored as `duplicate`, history gets `historical_reason` and is never auto-approved; reviewers can bulk-mark it `already_recorded` (posts to the ledger, never delivered).

**Tech Stack:** Python 3.12, FastAPI + Jinja2, SQLAlchemy 2.0 / Postgres (pgserver in tests), Alembic, BeautifulSoup + lxml, anthropic SDK, pytest via `uv`.

**Spec:** `docs/superpowers/specs/2026-10-03-threads-and-multi-invoice-pdfs-design.md` — Stage 1 only. Read its "Planning-time corrections" section: it supersedes the earlier sections where they differ.

## Global Constraints

- Message statuses: exactly `new`, `seen`, `no_content`, `failed`. `seen_reason`: `fingerprint` or `references_recorded`.
- Extraction statuses gain exactly `already_recorded` and `duplicate`.
- "Recorded" = extraction status in `pending_review`, `approved`, `already_recorded`, `duplicate`, **or** `rejected` with `is_remittance = false`. Payment-key **uniqueness** counts only `pending_review`, `approved`, `already_recorded`.
- Payment keys: `utr:<REF>` (types utr/rtgs/neft/imps, strong), `chq:<payer>:<REF>` (strong), `doc:<payer>:<REF>:<FY>` (types payer_document/request_number, strong, FY like `2025-26`), `soft:<payer>:<amount 2dp>:<date|nodate>` (weak). `<REF>` = uppercase alphanumerics only; `<payer>` = ledger payer words joined with `-`.
- Duplicate amount tolerance: ₹1.00.
- Historical flags start with `header: historical —`; the truncation flag is exactly `header: content was truncated — check nothing is missing`.
- Fingerprint: sha256 of the first 60 `[a-z0-9]+` tokens of the lower-cased body after removing "CAUTION: …" banners; `None` when fewer than 8 tokens. Applied for "seen" only when the message has a payment signal and does not carry the email's attachments.
- New settings (all optional, empty default): `CLIENT_DOMAINS` (comma list), `CLIENT_NAMES` (comma list), `GO_LIVE_DATE` (YYYY-MM-DD).
- AI calls: one per `new` message; system prompt sent as a cached block (`cache_control: ephemeral`; claude-opus-5 caches prefixes ≥ 512 tokens).
- Line length 100 (ruff). Tests: `uv run pytest <paths> -q`; lint `uv run ruff check ar_pipeline tests`; format `uv run ruff format --check ar_pipeline tests`; types `uv run mypy ar_pipeline`. The full suite takes ~25 s.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## File Structure

| File | Responsibility |
|---|---|
| `ar_pipeline/config.py` (modify) | `client_domains`, `client_names`, `go_live_date` settings + list helpers |
| `ar_pipeline/db/models.py` (modify) | `Email.thread_key`; `EmailMessage`; new columns on `ExtractionSource`, `Extraction`; statuses |
| `migrations/versions/0005_email_threads.py` (create) | schema for the above + partial unique index |
| `ar_pipeline/ingest/{types,gmail_client,client,eml,poller}.py` (modify) | carry `thread_key` from the mailbox into `email` |
| `ar_pipeline/extract/html_table.py` (modify) | expose `table_rows(tag)` for reuse |
| `ar_pipeline/threads/__init__.py` (create) | package marker |
| `ar_pipeline/threads/references.py` (create) | pure: `find_references`, `normalize_ref`, `payer_slug`, `payment_key_for`, `has_payment_signal` |
| `ar_pipeline/threads/splitter.py` (create) | pure: `MessagePart`, `split_email`, `fingerprint` |
| `ar_pipeline/threads/memory.py` (create) | DB: recorded predicate, seen-by-fingerprint, recorded references |
| `ar_pipeline/threads/dedupe.py` (create) | DB: `assign_payment_key`, `apply_history` |
| `ar_pipeline/pipeline/advance.py` (modify) | classify creates messages + per-message sources; extractor reads message content |
| `ar_pipeline/normalize/service.py` (modify) | per-message normalize, isolation, keys, history |
| `ar_pipeline/normalize/prompt.py` (modify) | `payer_document` type; `is_truncated` |
| `ar_pipeline/normalize/validators.py` (modify) | known reference types gain `payer_document` |
| `ar_pipeline/normalize/llm_client.py` (modify) | cached system prompt + cache usage logging |
| `ar_pipeline/review/service.py`, `app.py`, templates (modify) | already-recorded actions, thread view, badges, failed messages, outcome labels |
| `ar_pipeline/cli.py` (modify) | `threads-backfill` |
| `tests/threads/…` (create) | chain builders + tests per module |

---

### Task 1: Settings, models and migration

**Files:**
- Modify: `ar_pipeline/config.py`, `ar_pipeline/db/models.py`
- Create: `migrations/versions/0005_email_threads.py`
- Test: `tests/threads/__init__.py` (empty), `tests/threads/test_models.py`, `tests/test_config.py` (append)

**Interfaces — Produces:**
- `Settings.client_domains: str`, `client_names: str`, `go_live_date: date | None`; helpers `Settings.client_domain_list() -> list[str]` (lower-cased, stripped, empties dropped) and `Settings.client_name_list() -> list[str]`.
- `EmailMessage` model (table `email_message`); `Email.thread_key`; `ExtractionSource.email_message_id`; `Extraction.email_message_id`, `payment_key`, `payment_key_strength`, `historical_reason`, `duplicate_of_id`; `MESSAGE_STATUSES`; `EXTRACTION_STATUSES` with `already_recorded`, `duplicate`.

- [ ] **Step 1: Write the failing tests**

`tests/threads/test_models.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from ar_pipeline.db.models import Email, EmailMessage, Extraction


def _email(db_session, mid: str = "m-1") -> Email:
    e = Email(
        internet_message_id=mid, sender_address="a@b.com", sender_domain="b.com",
        subject="s", received_at=datetime(2026, 10, 1, tzinfo=UTC), status="new",
        thread_key="thread-1",
    )
    db_session.add(e)
    db_session.flush()
    return e


def test_email_message_round_trip(db_session):
    e = _email(db_session)
    m = EmailMessage(
        email_id=e.id, position=1, sender="str@alufluoride.com", raw_header="From: x",
        is_internal=False, carries_attachments=False, body_text="hi", tables=[["a", "b"]],
        fingerprint="f" * 64, status="new",
    )
    db_session.add(m)
    db_session.flush()
    db_session.refresh(m)
    assert m.tables == [["a", "b"]]
    assert m.seen_reason is None


def test_new_extraction_statuses_are_allowed(db_session):
    e = _email(db_session)
    for status in ("already_recorded", "duplicate"):
        db_session.add(Extraction(email_id=e.id, canonical={}, status=status))
    db_session.flush()


def test_strong_payment_key_is_unique_among_live_rows(db_session):
    e = _email(db_session)
    db_session.add(Extraction(email_id=e.id, canonical={}, status="approved",
                              payment_key="utr:X1", payment_key_strength="strong"))
    db_session.flush()
    db_session.add(Extraction(email_id=e.id, canonical={}, status="pending_review",
                              payment_key="utr:X1", payment_key_strength="strong"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_rejected_and_weak_keys_do_not_collide(db_session):
    e = _email(db_session)
    db_session.add_all([
        Extraction(email_id=e.id, canonical={}, status="rejected",
                   payment_key="utr:X2", payment_key_strength="strong"),
        Extraction(email_id=e.id, canonical={}, status="pending_review",
                   payment_key="utr:X2", payment_key_strength="strong"),
        Extraction(email_id=e.id, canonical={}, status="pending_review",
                   payment_key="soft:a:1.00:nodate", payment_key_strength="weak"),
        Extraction(email_id=e.id, canonical={}, status="pending_review",
                   payment_key="soft:a:1.00:nodate", payment_key_strength="weak"),
    ])
    db_session.flush()
```
Append to `tests/test_config.py`:
```python
def test_client_lists_and_go_live(monkeypatch):
    import datetime

    import ar_pipeline.config as config_module

    monkeypatch.setenv("CLIENT_DOMAINS", " AdityaBirla.com, ,hindalco.com ")
    monkeypatch.setenv("CLIENT_NAMES", "Hindalco Industries")
    monkeypatch.setenv("GO_LIVE_DATE", "2026-10-15")
    config_module.get_settings.cache_clear()
    try:
        s = config_module.get_settings()
        assert s.client_domain_list() == ["adityabirla.com", "hindalco.com"]
        assert s.client_name_list() == ["Hindalco Industries"]
        assert s.go_live_date == datetime.date(2026, 10, 15)
    finally:
        config_module.get_settings.cache_clear()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/threads/test_models.py tests/test_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'EmailMessage'` and settings attribute errors.

- [ ] **Step 3: Settings**

In `ar_pipeline/config.py` add `from datetime import date` and, after `auto_approve_min_confidence`, add:
```python
    # Threads: who "we" are (internal forwards, payer-vs-beneficiary) and the
    # cut-off before which payments are treated as already-recorded history.
    client_domains: str = ""
    client_names: str = ""
    go_live_date: date | None = None

    def client_domain_list(self) -> list[str]:
        return [d.strip().lower() for d in self.client_domains.split(",") if d.strip()]

    def client_name_list(self) -> list[str]:
        return [n.strip() for n in self.client_names.split(",") if n.strip()]
```
pydantic-settings treats an empty `GO_LIVE_DATE=` as an error for `date | None`; if `.env` may contain `GO_LIVE_DATE=` with no value, add a `field_validator("go_live_date", mode="before")` returning `None` for `""`.

- [ ] **Step 4: Models**

In `ar_pipeline/db/models.py`:
- Change `EXTRACTION_STATUSES` to `("pending_review", "approved", "rejected", "superseded", "already_recorded", "duplicate")` and add `MESSAGE_STATUSES = ("new", "seen", "no_content", "failed")`.
- `Email`: add `thread_key: Mapped[str | None] = mapped_column(Text)`.
- Add, after `Email`:
```python
class EmailMessage(Base):
    """One message found inside an email's body (a quoted reply or forward is
    its own message). Only ``new`` messages are sent to the AI."""

    __tablename__ = "email_message"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("email.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    sender: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_header: Mapped[str] = mapped_column(Text, default="")
    is_internal: Mapped[bool] = mapped_column(default=False)
    carries_attachments: Mapped[bool] = mapped_column(default=False)
    body_text: Mapped[str | None] = mapped_column(Text)
    tables: Mapped[list | None] = mapped_column(JSONB)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="new")
    seen_reason: Mapped[str | None] = mapped_column(String(30))
    seen_in_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("email_message.id"))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in("status", MESSAGE_STATUSES), name="ck_email_message_status"),
        UniqueConstraint("email_id", "position", name="uq_email_message_position"),
    )
```
- `ExtractionSource`: add `email_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("email_message.id"), index=True)`.
- `Extraction`: add
```python
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("email_message.id"), index=True
    )
    payment_key: Mapped[str | None] = mapped_column(Text)
    payment_key_strength: Mapped[str | None] = mapped_column(String(10))
    historical_reason: Mapped[str | None] = mapped_column(String(30))
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("extraction.id"))
```
  and to `Extraction.__table_args__` add:
```python
        Index(
            "ux_extraction_payment_key",
            "payment_key",
            unique=True,
            postgresql_where=text(
                "payment_key_strength = 'strong' AND "
                "status IN ('pending_review', 'approved', 'already_recorded')"
            ),
        ),
        Index("ix_extraction_payment_key", "payment_key"),
```
  (import `text` from `sqlalchemy`).

- [ ] **Step 5: Migration**

`migrations/versions/0005_email_threads.py` (revision `"0005"`, down `"0004"`):
```python
"""email threads: email_message, payment keys, history statuses"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "status IN ('pending_review', 'approved', 'rejected', 'superseded')"
_NEW = (
    "status IN ('pending_review', 'approved', 'rejected', 'superseded', "
    "'already_recorded', 'duplicate')"
)
_UNIQUE_WHERE = (
    "payment_key_strength = 'strong' AND "
    "status IN ('pending_review', 'approved', 'already_recorded')"
)


def upgrade() -> None:
    op.add_column("email", sa.Column("thread_key", sa.Text(), nullable=True))
    op.create_table(
        "email_message",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("sender", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_header", sa.Text(), nullable=False),
        sa.Column("is_internal", sa.Boolean(), nullable=False),
        sa.Column("carries_attachments", sa.Boolean(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("tables", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("seen_reason", sa.String(length=30), nullable=True),
        sa.Column("seen_in_message_id", sa.UUID(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('new', 'seen', 'no_content', 'failed')", name="ck_email_message_status"
        ),
        sa.ForeignKeyConstraint(["email_id"], ["email.id"]),
        sa.ForeignKeyConstraint(["seen_in_message_id"], ["email_message.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email_id", "position", name="uq_email_message_position"),
    )
    op.create_index(op.f("ix_email_message_email_id"), "email_message", ["email_id"])
    op.create_index(op.f("ix_email_message_fingerprint"), "email_message", ["fingerprint"])

    op.add_column("extraction_source", sa.Column("email_message_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        None, "extraction_source", "email_message", ["email_message_id"], ["id"]
    )
    op.create_index(
        op.f("ix_extraction_source_email_message_id"), "extraction_source", ["email_message_id"]
    )

    op.add_column("extraction", sa.Column("email_message_id", sa.UUID(), nullable=True))
    op.add_column("extraction", sa.Column("payment_key", sa.Text(), nullable=True))
    op.add_column(
        "extraction", sa.Column("payment_key_strength", sa.String(length=10), nullable=True)
    )
    op.add_column(
        "extraction", sa.Column("historical_reason", sa.String(length=30), nullable=True)
    )
    op.add_column("extraction", sa.Column("duplicate_of_id", sa.UUID(), nullable=True))
    op.create_foreign_key(None, "extraction", "email_message", ["email_message_id"], ["id"])
    op.create_foreign_key(None, "extraction", "extraction", ["duplicate_of_id"], ["id"])
    op.create_index(
        op.f("ix_extraction_email_message_id"), "extraction", ["email_message_id"]
    )
    op.create_index("ix_extraction_payment_key", "extraction", ["payment_key"])
    # every payment_key is NULL right now, so the unique index cannot fail;
    # `ar-pipeline threads-backfill` assigns keys afterwards, skipping conflicts.
    op.create_index(
        "ux_extraction_payment_key", "extraction", ["payment_key"], unique=True,
        postgresql_where=sa.text(_UNIQUE_WHERE),
    )
    op.drop_constraint("ck_extraction_status", "extraction", type_="check")
    op.create_check_constraint("ck_extraction_status", "extraction", _NEW)


def downgrade() -> None:
    # Forward-only assumption: fails if already_recorded/duplicate rows exist.
    op.drop_constraint("ck_extraction_status", "extraction", type_="check")
    op.create_check_constraint("ck_extraction_status", "extraction", _OLD)
    op.drop_index("ux_extraction_payment_key", table_name="extraction")
    op.drop_index("ix_extraction_payment_key", table_name="extraction")
    op.drop_index(op.f("ix_extraction_email_message_id"), table_name="extraction")
    op.drop_constraint("extraction_duplicate_of_id_fkey", "extraction", type_="foreignkey")
    op.drop_constraint("extraction_email_message_id_fkey", "extraction", type_="foreignkey")
    for col in ("duplicate_of_id", "historical_reason", "payment_key_strength",
                "payment_key", "email_message_id"):
        op.drop_column("extraction", col)
    op.drop_index(
        op.f("ix_extraction_source_email_message_id"), table_name="extraction_source"
    )
    op.drop_constraint(
        "extraction_source_email_message_id_fkey", "extraction_source", type_="foreignkey"
    )
    op.drop_column("extraction_source", "email_message_id")
    op.drop_index(op.f("ix_email_message_fingerprint"), table_name="email_message")
    op.drop_index(op.f("ix_email_message_email_id"), table_name="email_message")
    op.drop_table("email_message")
    op.drop_column("email", "thread_key")
```
Postgres names unnamed FKs `<table>_<column>_fkey`, which the downgrade relies on.

- [ ] **Step 6: Run tests, including migration drift**

Run: `uv run pytest tests/threads/test_models.py tests/test_config.py tests/db -q`
Expected: PASS. `tests/db/test_migrations.py::test_no_model_migration_drift` (alembic check) must pass — if it reports a difference, align the migration with the model (index names, nullability, server defaults).

- [ ] **Step 7: Commit**
```bash
git add ar_pipeline/config.py ar_pipeline/db/models.py migrations/versions/0005_email_threads.py tests/threads tests/test_config.py
git commit -m "feat(threads): email_message table, payment keys, history statuses"
```

---

### Task 2: Carry the mailbox thread id into `email.thread_key`

**Files:**
- Modify: `ar_pipeline/ingest/types.py`, `ar_pipeline/ingest/gmail_client.py`, `ar_pipeline/ingest/client.py`, `ar_pipeline/ingest/eml.py`, `ar_pipeline/ingest/poller.py`
- Test: `tests/ingest/test_thread_key.py`

**Interfaces — Produces:** `GraphMessage.thread_key: str | None = None` (last field, defaulted so existing constructors keep working); `Email.thread_key` populated by the poller and by `ingest_eml_file`.

- [ ] **Step 1: Write the failing tests**

`tests/ingest/test_thread_key.py`:
```python
from __future__ import annotations

from pathlib import Path

from ar_pipeline.ingest.eml import parse_eml
from ar_pipeline.ingest.gmail_client import GmailClient


def test_eml_thread_key_is_the_root_of_references(tmp_path: Path):
    p = tmp_path / "x.eml"
    p.write_text(
        "Message-ID: <c@x>\nReferences: <root@x> <b@x>\nIn-Reply-To: <b@x>\n"
        "From: a@b.com\nSubject: s\nDate: Wed, 18 Feb 2026 12:08:59 +0000\n\nbody\n"
    )
    msg, _ = parse_eml(p)
    assert msg.thread_key == "<root@x>"


def test_eml_without_references_uses_in_reply_to_then_own_id(tmp_path: Path):
    p = tmp_path / "y.eml"
    p.write_text("Message-ID: <solo@x>\nFrom: a@b.com\nSubject: s\n\nbody\n")
    msg, _ = parse_eml(p)
    assert msg.thread_key == "<solo@x>"


def test_gmail_thread_id_becomes_thread_key():
    client = GmailClient(auth=object())  # type: ignore[arg-type]
    try:
        msg = client._parse_message(  # noqa: SLF001 — parsing only, no network
            {"id": "m1", "threadId": "t-42", "internalDate": "0",
             "payload": {"headers": [{"name": "Message-Id", "value": "<m@x>"}]}}
        )
    finally:
        client.close()
    assert msg.thread_key == "t-42"
```
Check the method name used to parse one Gmail message in `gmail_client.py` (the function that builds the `GraphMessage` at line ~207) and use that name here; likewise the Graph `_parse_message` gets `item.get("conversationId")`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/ingest/test_thread_key.py -q`
Expected: FAIL — `GraphMessage` has no `thread_key`.

- [ ] **Step 3: Implement**
- `types.py`: add `thread_key: str | None = None` as the last field of `GraphMessage`.
- `gmail_client.py`: in the parsed-message constructor pass `thread_key=item.get("threadId")`.
- `client.py` (Graph): pass `thread_key=item.get("conversationId")` in the non-removed branch.
- `eml.py`: compute
```python
    refs = (msg["References"] or "").split()
    in_reply_to = (msg["In-Reply-To"] or "").strip()
    thread_key = refs[0] if refs else (in_reply_to or internet_message_id)
```
  pass `thread_key=thread_key` to `GraphMessage(...)`, and set `thread_key=message.thread_key` on the `Email(...)` in `ingest_eml_file`.
- `poller.py`: set `thread_key=message.thread_key` on the `Email(...)`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ingest -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/ingest tests/ingest/test_thread_key.py
git commit -m "feat(threads): store the mailbox thread id on each email"
```

---

### Task 3: References and payment keys (pure)

**Files:**
- Create: `ar_pipeline/threads/__init__.py` (docstring `"""Email threads: splitting, thread memory, payment keys."""`), `ar_pipeline/threads/references.py`
- Modify: `ar_pipeline/ledger/matching.py` (public `payer_slug`), `ar_pipeline/normalize/prompt.py`, `ar_pipeline/normalize/validators.py`
- Test: `tests/threads/test_references.py`

**Interfaces — Produces:**
- `normalize_ref(raw: str) -> str`
- `find_references(text: str) -> set[str]` (normalized, keyword-anchored only)
- `has_payment_signal(text: str, tables: list[list[list[str]]] | None = None) -> bool`
- `payer_slug(name: str | None) -> str` (in `ledger/matching.py`; `""` when unknown)
- `payment_key_for(canonical: dict) -> tuple[str, str] | None` → `(key, "strong"|"weak")`
- `canonical_total(canonical: dict) -> Decimal | None`

- [ ] **Step 1: Write the failing tests**

`tests/threads/test_references.py`:
```python
from decimal import Decimal

import pytest

from ar_pipeline.ledger.matching import payer_slug
from ar_pipeline.threads.references import (
    canonical_total, find_references, has_payment_signal, normalize_ref, payment_key_for,
)


@pytest.mark.parametrize(
    ("text", "refs"),
    [
        ("vide UTR no.PUNBR52026021813360279 on 18.02", {"PUNBR52026021813360279"}),
        ("UTR: HDFC52026092700118", {"HDFC52026092700118"}),
        ("SWIFT/UTR Reference: BARC20260928X4471", {"BARC20260928X4471"}),
        ("NEFT ref - SBIN0012345678", {"SBIN0012345678"}),
        ("RTGS/NEFT Reference : RTGS PAYMENT", set()),
        ("Invoice JHMUR2510007033 for 1,452,299.16", set()),
    ],
)
def test_find_references(text, refs):
    assert find_references(text) == refs


def test_normalize_ref():
    assert normalize_ref(" hdfc-5202 6092 ") == "HDFC52026092"


@pytest.mark.parametrize(
    ("text", "signal"),
    [("Rs.1,36,70,691.00 paid", True), ("UTR: HDFC52026092700118", True),
     ("Dear All, Please applied below payment.", False), ("", False)],
)
def test_has_payment_signal(text, signal):
    assert has_payment_signal(text) is signal


def test_table_with_amounts_is_a_signal():
    assert has_payment_signal("", [[["Invoice", "Amount"], ["X1", "1,000.00"]]])


def test_payer_slug():
    assert payer_slug("Global TVS Bus Body Builders Ltd.") == "global-tvs-bus-body-builders"
    assert payer_slug(None) == ""


def _c(ref=None, rtype=None, payer="Acme Corp", total="100.00", date="2026-02-18"):
    return {"header": {"payment_reference": ref, "payment_reference_type": rtype,
                       "payer_name": payer, "total_paid_amount": total, "payment_date": date}}


@pytest.mark.parametrize(
    ("canonical", "expected"),
    [
        (_c("hdfc-1234567890", "utr"), ("utr:HDFC1234567890", "strong")),
        (_c("SBIN99", "neft"), ("utr:SBIN99", "strong")),
        (_c("000123", "cheque"), ("chq:acme:000123", "strong")),
        (_c("1500005408", "payer_document", date="2026-01-17"),
         ("doc:acme:1500005408:2025-26", "strong")),
        (_c("1500005408", "payer_document", date="2026-04-02"),
         ("doc:acme:1500005408:2026-27", "strong")),
        (_c("ZZ1", "something_else"), ("soft:acme:100.00:2026-02-18", "weak")),
        (_c(None, None, date=None), ("soft:acme:100.00:nodate", "weak")),
        (_c(None, None, payer=""), None),
    ],
)
def test_payment_key_for(canonical, expected):
    assert payment_key_for(canonical) == expected


def test_canonical_total():
    assert canonical_total(_c(total="1,000.50".replace(",", ""))) == Decimal("1000.50")
    assert canonical_total({}) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/threads/test_references.py -q`
Expected: FAIL — `ModuleNotFoundError: ar_pipeline.threads`.

- [ ] **Step 3: Implement**

In `ar_pipeline/ledger/matching.py` add below `_payer_words`:
```python
def payer_slug(name: str | None) -> str:
    """Payer identity for keys: the normalised payer words joined by '-'."""
    return _payer_words(name).replace(" ", "-")
```

`ar_pipeline/threads/references.py`:
```python
"""Bank references, payment signals and payment keys. Pure: no DB, no I/O."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from ar_pipeline.ledger.matching import payer_slug

# keyword-anchored only: bare code-shaped tokens include invoice numbers
# (JHMUR2510007033), which would make the "all references recorded" check
# never fire.
_REF_RE = re.compile(
    r"(?i)\b(?:UTR|RTGS|NEFT|IMPS)\b(?:\s*(?:no|number|ref(?:erence)?)\.?)?"
    r"\s*[:#.\-]?\s*((?=[A-Z0-9 \-]*\d)[A-Z0-9][A-Z0-9\-]{6,30}[A-Z0-9])"
)
_AMOUNT_RE = re.compile(
    r"(?<!\d[./])\b\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?\b|(?<![\d.])\d+\.\d{2}(?![.\d])"
)
_BANK_TYPES = {"utr", "rtgs", "neft", "imps"}
_DOC_TYPES = {"payer_document", "request_number"}


def normalize_ref(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", raw.upper())


def find_references(text: str) -> set[str]:
    refs = {normalize_ref(m.group(1)) for m in _REF_RE.finditer(text or "")}
    return {r for r in refs if any(c.isdigit() for c in r) and len(r) >= 8}


def has_payment_signal(text: str, tables: list[list[list[str]]] | None = None) -> bool:
    if find_references(text) or _AMOUNT_RE.search(text or ""):
        return True
    for table in tables or []:
        for row in table:
            if any(_AMOUNT_RE.search(cell or "") for cell in row):
                return True
    return False


def canonical_total(canonical: dict) -> Decimal | None:
    raw = ((canonical or {}).get("header") or {}).get("total_paid_amount")
    try:
        return Decimal(str(raw)) if raw not in (None, "") else None
    except InvalidOperation:
        return None


def _financial_year(raw: object) -> str:
    try:
        d = date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return "nofy"
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def payment_key_for(canonical: dict) -> tuple[str, str] | None:
    header = (canonical or {}).get("header") or {}
    ref = normalize_ref(str(header.get("payment_reference") or ""))
    rtype = str(header.get("payment_reference_type") or "").lower()
    payer = payer_slug(header.get("payer_name"))
    if ref and rtype in _BANK_TYPES:
        return f"utr:{ref}", "strong"
    if ref and payer and rtype == "cheque":
        return f"chq:{payer}:{ref}", "strong"
    if ref and payer and rtype in _DOC_TYPES:
        return f"doc:{payer}:{ref}:{_financial_year(header.get('payment_date'))}", "strong"
    total = canonical_total(canonical)
    if payer and total is not None:
        when = str(header.get("payment_date") or "nodate")
        return f"soft:{payer}:{total:.2f}:{when}", "weak"
    return None
```
Note the `has_payment_signal("", [[["Invoice","Amount"],["X1","1,000.00"]]])` test passes a list-of-tables shape `[table]`; the function takes `tables: list[table]` where `table = list[row]` — the test's outer list is that list of tables.

`ar_pipeline/normalize/validators.py`: add `"payer_document"` to `_KNOWN_REFERENCE_TYPES`.
`ar_pipeline/normalize/prompt.py`: in the system prompt sentence listing `payment_reference_type` values, add `"payer_document"` and one sentence after it: `When the advice has no bank reference but shows the payer's own document number (e.g. "Document No : 1500005408"), put that number in \`payment_reference\` with type "payer_document".` Bump `PROMPT_VERSION` to `"4"` and update the two tests that assert `"3"` (`grep -rn 'prompt_version == "3"\|PROMPT_VERSION' tests`).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/threads/test_references.py tests/normalize -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/threads ar_pipeline/ledger/matching.py ar_pipeline/normalize tests/threads/test_references.py tests/normalize tests/pipeline
git commit -m "feat(threads): bank references and payment keys"
```

---

### Task 4: Thread splitter (pure)

**Files:**
- Create: `ar_pipeline/threads/splitter.py`, `tests/threads/chains.py` (fixture builders), `tests/threads/test_splitter.py`
- Modify: `ar_pipeline/extract/html_table.py` (extract `table_rows`)

**Interfaces — Produces:**
```python
@dataclass(frozen=True)
class MessagePart:
    position: int
    sender: str | None          # lower-cased address if known, else the raw display name
    sent_at: datetime | None    # tz-aware; quoted local times without a zone are stored as UTC
    raw_header: str             # "" for the top message
    body_text: str
    tables: list[list[list[str]]]
    is_internal: bool
    has_payment_signal: bool
    fingerprint: str | None

def split_email(*, body_html: str, body_text: str, sender: str, received_at: datetime,
                client_domains: list[str]) -> list[MessagePart]
def fingerprint(body_text: str) -> str | None
```
`html_table.table_rows(table: Tag) -> list[list[str]]`.

- [ ] **Step 1: Chain builders for tests**

`tests/threads/chains.py`:
```python
"""Synthetic, redacted thread fixtures in the shapes seen in real AR mail."""

from __future__ import annotations


def remittance_html(n: int) -> str:
    amount = f"{(n + 1) * 1000:,}.00"
    return (
        "<p>Dear Dharmendra Ji,</p>"
        f"<p>We have remitted the payment of Rs.{amount} vide UTR no.PUNBR52026{n:011d} "
        f"on {10 + n:02d}.01.2026. Please find the below invoice details for your reference.</p>"
        "<table><tr><th>Invoice Number</th><th>Original</th></tr>"
        f"<tr><td>JHMUR25100{n:05d}</td><td>{amount}</td></tr></table>"
        "<p>Thanks &amp; Regards<br>S Tejeswara Rao<br>Alufluoride Limited</p>"
    )


def outlook_header_html(sender: str, sent: str) -> str:
    return (
        '<div style="border:none;border-top:solid #E1E1E1 1.0pt">'
        f"<p><b>From:</b> {sender}<br><b>Sent:</b> {sent}<br>"
        "<b>To:</b> Dharmendra Kumar<br><b>Subject:</b> Re: Payment Remittance details</p></div>"
    )


def chain_html(payments: list[int], *, top_note: str = "Dear All, Please applied below payment.") -> str:
    """Newest payment first, like a real reply chain, under an internal forward note."""
    parts = [f"<p>{top_note}</p><p>Regards,<br>Dharmendra</p>"]
    for i, n in enumerate(payments):
        sender = "Tejeswara Rao S" if i % 2 else "Tejeswara Rao S &lt;STR@alufluoride.com&gt;"
        parts.append(outlook_header_html(sender, f"{10 + n:02d} January 2026 03:24 PM"))
        parts.append(remittance_html(n))
    return "<html><body>" + "".join(parts) + "</body></html>"


def chain_text(payments: list[int]) -> str:
    out = ["Dear All,", "Please applied below payment.", "", "Regards,", "Dharmendra", ""]
    for n in payments:
        amount = f"{(n + 1) * 1000:,}.00"
        out += [
            "From: Tejeswara Rao S <STR@alufluoride.com>",
            f"Sent: {10 + n:02d} January 2026 03:24 PM",
            "To: Dharmendra Kumar",
            "Subject: Re: Payment Remittance details",
            "",
            "Dear Dharmendra Ji,",
            f"We have remitted the payment of Rs.{amount} vide UTR no.PUNBR52026{n:011d}.",
            f"Invoice JHMUR25100{n:05d}  {amount}",
            "Thanks & Regards", "Alufluoride Limited", "",
        ]
    return "\n".join(out)


GMAIL_REPLY_TEXT = (
    "Thanks, received.\n\n"
    "On Wed, Feb 18, 2026 at 5:07 PM Tejeswara Rao <str@alufluoride.com> wrote:\n"
    "> We have remitted Rs.1,000.00 vide UTR no.PUNBR52026000000000001.\n"
)

FORWARD_BANNER_TEXT = (
    "FYI\n\n"
    "-------- Forwarded Message --------\n"
    "Subject:\tFW: Payment Remittance details\n"
    "Date:\tWed, 18 Feb 2026 12:08:59 +0000\n"
    "From:\tDharmendra Kumar <dharmendra.p.kumar-c@adityabirla.com>\n"
    "To:\tShwetha M <shwetha.m@adityabirla.com>\n"
    "\n"
    "Dear All,\nPlease applied below payment.\n"
)
```

- [ ] **Step 2: Write the failing tests**

`tests/threads/test_splitter.py`:
```python
from __future__ import annotations

import re
from datetime import UTC, datetime

from ar_pipeline.threads.splitter import fingerprint, split_email
from tests.threads.chains import (
    FORWARD_BANNER_TEXT, GMAIL_REPLY_TEXT, chain_html, chain_text, remittance_html,
)

T0 = datetime(2026, 2, 18, 12, 8, tzinfo=UTC)
DOMAINS = ["adityabirla.com"]


def _split(html="", text="", sender="dharmendra.p.kumar-c@adityabirla.com"):
    return split_email(body_html=html, body_text=text, sender=sender, received_at=T0,
                       client_domains=DOMAINS)


def _nonws(s: str) -> str:
    return re.sub(r"\s+", "", s)


def test_outlook_html_chain_splits_into_one_part_per_message():
    parts = _split(html=chain_html([3, 2, 1]))
    assert [p.position for p in parts] == [0, 1, 2, 3]
    top = parts[0]
    assert top.is_internal and not top.has_payment_signal
    assert parts[1].sender == "str@alufluoride.com"
    assert parts[2].sender == "tejeswara rao s"  # bare display name in this copy
    for p in parts[1:]:
        assert p.has_payment_signal and len(p.tables) == 1
        assert "From:" in p.raw_header and "Sent:" in p.raw_header
    assert parts[1].sent_at is not None and parts[1].sent_at.day == 13


def test_plain_text_chain():
    parts = _split(text=chain_text([2, 1]))
    assert len(parts) == 3
    assert "PUNBR52026" in parts[1].body_text and parts[1].tables == []


def test_nothing_is_lost():
    html = chain_html([3, 2, 1])
    from bs4 import BeautifulSoup

    expected = _nonws(BeautifulSoup(html, "lxml").get_text())
    got = _nonws("".join(p.raw_header + p.body_text for p in _split(html=html)))
    assert got == expected


def test_gmail_reply():
    parts = _split(text=GMAIL_REPLY_TEXT, sender="ar@adityabirla.com")
    assert len(parts) == 2
    assert parts[1].sender == "str@alufluoride.com"
    assert parts[1].has_payment_signal


def test_forward_banner():
    parts = _split(text=FORWARD_BANNER_TEXT, sender="me@example.com")
    assert len(parts) == 2
    assert parts[1].sender == "dharmendra.p.kumar-c@adityabirla.com"
    assert parts[1].is_internal
    assert parts[1].sent_at == datetime(2026, 2, 18, 12, 8, 59, tzinfo=UTC)


def test_no_quotes_is_one_message():
    parts = _split(text="Payment of INR 1,000.00 made. UTR: HDFC52026092700118",
                   sender="ap@payer.example")
    assert len(parts) == 1 and parts[0].position == 0 and not parts[0].is_internal


def test_empty_body_gives_no_parts():
    assert _split() == []


def test_fingerprint_is_stable_across_html_and_reformatting():
    a = _split(html=chain_html([5]))[1]
    b = _split(html=chain_html([7, 5]))[2]  # same message quoted one level deeper
    assert a.fingerprint is not None and a.fingerprint == b.fingerprint


def test_fingerprint_ignores_caution_banner_and_quote_markers():
    body = remittance_html(1)
    from bs4 import BeautifulSoup

    text = BeautifulSoup(body, "lxml").get_text("\n")
    banner = "CAUTION: This email originated from outside of the organization. Do not click links or open attachments unless you recognize the sender and know the content is safe.\n"
    quoted = "\n".join("> " + line for line in text.splitlines())
    assert fingerprint(text) == fingerprint(banner + quoted)


def test_short_text_has_no_fingerprint():
    assert fingerprint("Please find attached.") is None
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/threads/test_splitter.py -q`
Expected: FAIL — `ModuleNotFoundError: ar_pipeline.threads.splitter`.

- [ ] **Step 4: Extract `table_rows` in `html_table.py`**

Move the row-building loop into:
```python
def table_rows(table: Tag) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        if not isinstance(tr, Tag):
            continue
        rows.append([_cell_text(c) for c in tr.find_all(["td", "th"]) if isinstance(c, Tag)])
    return rows
```
and call `tables.append(table_rows(table))` from `extract_html_tables`. Run `uv run pytest tests/extract -q` — unchanged behaviour.

- [ ] **Step 5: Implement the splitter**

`ar_pipeline/threads/splitter.py`:
```python
"""Split one email body into the messages of its thread (spec §2). Pure.

Boundaries are quoted-header blocks: Outlook ``From: / Sent:`` blocks, Gmail
``On … wrote:`` lines and ``Forwarded message`` banners. HTML is walked once
into a linear text (block elements become newlines) so the same patterns work
for HTML and plain text, and each innermost ``<table>`` is assigned to the
message whose text span contains it. Nothing is dropped: the parts' headers
and bodies, concatenated, are the whole linear text.
"""

from __future__ import annotations

import email.utils
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ar_pipeline.extract.html_table import table_rows
from ar_pipeline.threads.references import has_payment_signal

_BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "table", "blockquote", "h1", "h2", "h3", "h4", "h5",
    "h6", "hr", "pre", "section", "ul", "ol",
}
_SKIP_TAGS = {"script", "style", "head", "title"}
_LEAD = r"[ \t>*]*"
_OUTLOOK_RE = re.compile(
    rf"(?mi)^{_LEAD}From:[ \t*]*(?P<sender>[^\n]+)\n"
    rf"{_LEAD}(?:Sent|Date):[ \t*]*(?P<sent>[^\n]+)\n"
    rf"(?:{_LEAD}(?:To|Cc|Bcc|Subject|Importance):[^\n]*(?:\n|$))+"
)
_GMAIL_RE = re.compile(
    r"(?mi)^[ \t>]*On (?P<sent>[^\n]+?),?\s*(?P<sender>[^\n,]*<[^>\n]+>)\s*wrote:[ \t]*$\n?"
)
_FORWARD_RE = re.compile(
    r"(?mi)^[ \t>]*-{2,}\s*Forwarded message\s*-{2,}[ \t]*\n"
    r"(?P<hdrs>(?:[ \t>]*(?:From|Date|Sent|Subject|To|Cc):[^\n]*(?:\n|$))+)"
)
_HDR_FIELD_RE = re.compile(r"(?mi)^[ \t>]*(?P<name>From|Date|Sent):[ \t]*(?P<value>[^\n]+)")
_CAUTION_RE = re.compile(r"(?is)caution:.{0,300}?\bsafe\b\.?")
_WORD_RE = re.compile(r"[a-z0-9]+")
_DATE_FORMATS = (
    "%d %B %Y %H:%M", "%d %B %Y %I:%M %p", "%d %b %Y %H:%M", "%d %b %Y %I:%M %p",
    "%A, %B %d, %Y %I:%M %p", "%A, %d %B %Y %H:%M", "%A, %d %B, %Y %I:%M %p",
    "%a, %b %d, %Y at %I:%M %p", "%a, %b %d, %Y",
)
_FINGERPRINT_WORDS = 60
_MIN_FINGERPRINT_WORDS = 8


@dataclass(frozen=True)
class MessagePart:
    position: int
    sender: str | None
    sent_at: datetime | None
    raw_header: str
    body_text: str
    tables: list[list[list[str]]]
    is_internal: bool
    has_payment_signal: bool
    fingerprint: str | None


@dataclass(frozen=True)
class _Boundary:
    start: int
    end: int
    sender: str | None
    sent: str | None


def fingerprint(body_text: str) -> str | None:
    text = _CAUTION_RE.sub(" ", (body_text or "").lower())
    words = _WORD_RE.findall(text)
    if len(words) < _MIN_FINGERPRINT_WORDS:
        return None
    return hashlib.sha256(" ".join(words[:_FINGERPRINT_WORDS]).encode()).hexdigest()


def _linearize_html(html: str) -> tuple[str, list[tuple[int, Tag]]]:
    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    pos = 0
    tables: list[tuple[int, Tag]] = []

    def emit(s: str) -> None:
        nonlocal pos
        out.append(s)
        pos += len(s)

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                s = re.sub(r"\s+", " ", str(child))
                if s.strip():
                    emit(s)
                continue
            if not isinstance(child, Tag) or child.name in _SKIP_TAGS:
                continue
            block = child.name in _BLOCK_TAGS
            if block:
                emit("\n")
            if child.name == "table" and child.find("table") is None:
                tables.append((pos, child))
            if child.name in ("td", "th"):
                emit(" ")
            walk(child)
            if block:
                emit("\n")

    walk(soup)
    return "".join(out), tables


def _parse_sender(raw: str | None) -> str | None:
    if not raw:
        return None
    name, addr = email.utils.parseaddr(raw.replace("&lt;", "<").replace("&gt;", ">"))
    if "@" in addr:
        return addr.strip().lower()
    cleaned = (name or raw).strip().strip("*").strip()
    return cleaned.lower() or None


def _parse_sent(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        for fmt in _DATE_FORMATS:
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _boundaries(text: str) -> list[_Boundary]:
    found: list[_Boundary] = []
    for m in _OUTLOOK_RE.finditer(text):
        found.append(_Boundary(m.start(), m.end(), m.group("sender"), m.group("sent")))
    for m in _GMAIL_RE.finditer(text):
        found.append(_Boundary(m.start(), m.end(), m.group("sender"), m.group("sent")))
    for m in _FORWARD_RE.finditer(text):
        fields = {f.group("name").lower(): f.group("value") for f in _HDR_FIELD_RE.finditer(m.group("hdrs"))}
        found.append(
            _Boundary(m.start(), m.end(), fields.get("from"), fields.get("date") or fields.get("sent"))
        )
    found.sort(key=lambda b: (b.start, -b.end))
    kept: list[_Boundary] = []
    for b in found:  # a Gmail forward matches both banner and From/Date: keep the wider one
        if kept and b.start < kept[-1].end:
            continue
        kept.append(b)
    return kept


def _is_internal(sender: str | None, client_domains: list[str]) -> bool:
    if not sender or "@" not in sender:
        return False
    domain = sender.rsplit("@", 1)[1]
    return any(domain == d or domain.endswith("." + d) for d in client_domains)


def split_email(
    *, body_html: str, body_text: str, sender: str, received_at: datetime,
    client_domains: list[str],
) -> list[MessagePart]:
    if body_html and body_html.strip():
        text, table_tags = _linearize_html(body_html)
    else:
        text, table_tags = (body_text or "").replace("\r\n", "\n"), []
    if not text.strip():
        return []

    bounds = _boundaries(text)
    spans: list[tuple[int, int, str, str | None, datetime | None]] = []
    first = bounds[0].start if bounds else len(text)
    spans.append((0, first, "", _parse_sender(sender), received_at))
    for i, b in enumerate(bounds):
        end = bounds[i + 1].start if i + 1 < len(bounds) else len(text)
        spans.append((b.start, end, text[b.start:b.end], _parse_sender(b.sender), _parse_sent(b.sent)))

    parts: list[MessagePart] = []
    for start, end, header, who, when in spans:
        body_start = start + len(header)
        body = text[body_start:end]
        if not header and not body.strip():
            continue  # empty top: the thread starts with a quoted header
        tables = [table_rows(t) for p, t in table_tags if body_start <= p < end]
        parts.append(
            MessagePart(
                position=len(parts), sender=who, sent_at=when, raw_header=header,
                body_text=body, tables=tables, is_internal=_is_internal(who, client_domains),
                has_payment_signal=has_payment_signal(body, tables),
                fingerprint=fingerprint(body),
            )
        )
    return parts
```
If a test fails on a specific pattern, adjust the regex — not the test — unless the test contradicts the spec; record any such change in the report.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/threads tests/extract -q`
Expected: PASS.

- [ ] **Step 7: Commit**
```bash
git add ar_pipeline/threads/splitter.py ar_pipeline/extract/html_table.py tests/threads
git commit -m "feat(threads): split emails into the messages of their thread"
```

---

### Task 5: Thread memory and classify/extract integration

**Files:**
- Create: `ar_pipeline/threads/memory.py`
- Modify: `ar_pipeline/pipeline/advance.py`
- Test: `tests/threads/test_memory.py`, `tests/threads/test_classify_threads.py`

**Interfaces:**
- Consumes: `split_email`, `MessagePart` (Task 4); `find_references` (Task 3); `EmailMessage` (Task 1).
- Produces (`threads/memory.py`):
  - `RECORDED_FOR_SEEN = ("pending_review", "approved", "already_recorded", "duplicate")`
  - `is_recorded(ext: Extraction) -> bool` (also true for `rejected` with `is_remittance` false)
  - `message_is_recorded(session, message_id) -> bool` (≥1 extraction, all recorded)
  - `find_seen_by_fingerprint(session, *, email_id, fingerprint) -> EmailMessage | None`
  - `all_references_recorded(session, refs: set[str]) -> bool` (non-empty and every `utr:<ref>` key belongs to a live strong-key row)
- Produces (`advance.py`): `_ensure_messages(session, email, attachments) -> list[EmailMessage]`; classify writes `ExtractionSource.email_message_id`; extractor reads message content for message-bound body sources; an email whose messages are all `seen`/`no_content` with no live source becomes `done`.

- [ ] **Step 1: Write the failing tests**

`tests/threads/test_memory.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime

from ar_pipeline.db.models import Email, EmailMessage, Extraction
from ar_pipeline.threads.memory import (
    all_references_recorded, find_seen_by_fingerprint, message_is_recorded,
)


def _email(db_session, mid):
    e = Email(internet_message_id=mid, sender_address="a@b.com", sender_domain="b.com",
              subject="s", received_at=datetime(2026, 10, 1, tzinfo=UTC), status="review")
    db_session.add(e)
    db_session.flush()
    return e


def _msg(db_session, email, fp="f" * 64, status="new"):
    m = EmailMessage(email_id=email.id, position=1, raw_header="", is_internal=False,
                     carries_attachments=False, fingerprint=fp, status=status)
    db_session.add(m)
    db_session.flush()
    return m


def _ext(db_session, email, msg, status, *, remit=True, key=None):
    x = Extraction(email_id=email.id, email_message_id=msg.id, canonical={}, status=status,
                   is_remittance=remit, payment_key=key,
                   payment_key_strength="strong" if key else None)
    db_session.add(x)
    db_session.flush()
    return x


def test_recorded_rules(db_session):
    e = _email(db_session, "m1")
    m = _msg(db_session, e)
    assert not message_is_recorded(db_session, m.id)  # no extraction yet
    _ext(db_session, e, m, "pending_review")
    assert message_is_recorded(db_session, m.id)
    _ext(db_session, e, m, "rejected")  # bad extraction rejected -> not recorded
    assert not message_is_recorded(db_session, m.id)


def test_not_a_remittance_rejection_counts(db_session):
    e = _email(db_session, "m2")
    m = _msg(db_session, e)
    _ext(db_session, e, m, "rejected", remit=False)
    assert message_is_recorded(db_session, m.id)


def test_seen_by_fingerprint_skips_own_email_and_unrecorded(db_session):
    first = _email(db_session, "m3")
    m1 = _msg(db_session, first, fp="a" * 64)
    second = _email(db_session, "m4")
    assert find_seen_by_fingerprint(db_session, email_id=second.id, fingerprint="a" * 64) is None
    _ext(db_session, first, m1, "approved")
    assert find_seen_by_fingerprint(db_session, email_id=second.id, fingerprint="a" * 64).id == m1.id
    assert find_seen_by_fingerprint(db_session, email_id=first.id, fingerprint="a" * 64) is None


def test_all_references_recorded(db_session):
    e = _email(db_session, "m5")
    m = _msg(db_session, e)
    _ext(db_session, e, m, "approved", key="utr:REF11111111")
    assert all_references_recorded(db_session, {"REF11111111"})
    assert not all_references_recorded(db_session, {"REF11111111", "REF22222222"})
    assert not all_references_recorded(db_session, set())
```

`tests/threads/test_classify_threads.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime

import ar_pipeline.config as config_module
from sqlalchemy import select

from ar_pipeline.db.models import Email, EmailMessage, ExtractionSource
from ar_pipeline.pipeline.advance import advance_once
from ar_pipeline.storage import LocalBlobStore
from tests.extract.vision_fake import FakeVisionExtractor
from tests.normalize.llm_fake import FakeLLMClient
from tests.threads.chains import chain_html


def _email(db_session, html, mid="m-chain-1"):
    e = Email(internet_message_id=mid, sender_address="dharmendra.p.kumar-c@adityabirla.com",
              sender_domain="adityabirla.com", subject="FW: Payment Remittance details",
              received_at=datetime(2026, 2, 18, 12, 8, tzinfo=UTC), body_html=html,
              body_text="", status="new")
    db_session.add(e)
    db_session.flush()
    return e


def test_classify_splits_and_creates_one_source_per_new_message(db_session, tmp_path, monkeypatch):
    monkeypatch.setenv("CLIENT_DOMAINS", "adityabirla.com")
    config_module.get_settings.cache_clear()
    try:
        e = _email(db_session, chain_html([3, 2, 1]))
        advance_once(db_session, LocalBlobStore(str(tmp_path)), FakeVisionExtractor(), FakeLLMClient())
        msgs = db_session.scalars(
            select(EmailMessage).where(EmailMessage.email_id == e.id).order_by(EmailMessage.position)
        ).all()
        assert [m.status for m in msgs] == ["no_content", "new", "new", "new"]
        srcs = db_session.scalars(select(ExtractionSource).where(ExtractionSource.email_id == e.id)).all()
        assert sorted(s.kind for s in srcs) == ["body_table"] * 3
        assert {s.email_message_id for s in srcs} == {m.id for m in msgs[1:]}
        assert msgs[0].body_text is not None  # no_content still keeps its own text
    finally:
        config_module.get_settings.cache_clear()


def test_extract_reads_the_message_not_the_whole_email(db_session, tmp_path):
    e = _email(db_session, chain_html([2, 1]), mid="m-chain-2")
    store = LocalBlobStore(str(tmp_path))
    advance_once(db_session, store, FakeVisionExtractor(), FakeLLMClient())  # classify
    advance_once(db_session, store, FakeVisionExtractor(), FakeLLMClient())  # extract
    from ar_pipeline.db.models import RawExtraction

    payloads = db_session.scalars(
        select(RawExtraction.payload).join(ExtractionSource).where(ExtractionSource.email_id == e.id)
    ).all()
    assert len(payloads) == 2
    for p in payloads:
        assert p["text"].count("PUNBR52026") == 1  # each source holds one message
        assert len(p["tables"]) == 1
```
Note: with `CLIENT_DOMAINS` unset in the second test, the top note is not internal; it has no payment signal and no table, so classify gives it no source (prose under 120 chars) — still 2 sources.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/threads/test_memory.py tests/threads/test_classify_threads.py -q`
Expected: FAIL — `ModuleNotFoundError: ar_pipeline.threads.memory`.

- [ ] **Step 3: Implement `threads/memory.py`**
```python
"""Thread memory: has this message, or every reference it names, been recorded?"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import EmailMessage, Extraction

RECORDED_FOR_SEEN = ("pending_review", "approved", "already_recorded", "duplicate")
KEY_LIVE = ("pending_review", "approved", "already_recorded")


def is_recorded(ext: Extraction) -> bool:
    if ext.status in RECORDED_FOR_SEEN:
        return True
    return ext.status == "rejected" and not ext.is_remittance


def message_is_recorded(session: Session, message_id: uuid.UUID) -> bool:
    rows = list(session.scalars(select(Extraction).where(Extraction.email_message_id == message_id)))
    return bool(rows) and all(is_recorded(r) for r in rows)


def find_seen_by_fingerprint(
    session: Session, *, email_id: uuid.UUID, fingerprint: str
) -> EmailMessage | None:
    candidates = session.scalars(
        select(EmailMessage)
        .where(
            EmailMessage.fingerprint == fingerprint,
            EmailMessage.status == "new",
            EmailMessage.email_id != email_id,
        )
        .order_by(EmailMessage.created_at.asc(), EmailMessage.id.asc())
    )
    for cand in candidates:
        if message_is_recorded(session, cand.id):
            return cand
    return None


def all_references_recorded(session: Session, refs: set[str]) -> bool:
    if not refs:
        return False
    keys = {f"utr:{r}" for r in refs}
    found = set(
        session.scalars(
            select(Extraction.payment_key).where(
                Extraction.payment_key.in_(keys),
                Extraction.payment_key_strength == "strong",
                Extraction.status.in_(KEY_LIVE),
            )
        )
    )
    return found == keys
```

- [ ] **Step 4: Integrate into `advance.py`**

Add imports: `from ar_pipeline.config import get_settings`, `from ar_pipeline.db.models import EmailMessage`, `from ar_pipeline.threads.memory import all_references_recorded, find_seen_by_fingerprint`, `from ar_pipeline.threads.references import find_references`, `from ar_pipeline.threads.splitter import split_email`, `from ar_pipeline.classify.classifier import SourceSpec, has_numeric_table_rows` (see below), `from ar_pipeline.extract.base import ExtractedContent`.

In `ar_pipeline/classify/classifier.py` add a small public helper reused here:
```python
def has_numeric_table_rows(tables: list[list[list[str]]]) -> bool:
    for table in tables:
        cells = [c for row in table for c in row]
        if sum(1 for c in cells if _NUMERIC_CELL_RE.search(c)) >= 2:
            return True
    return False
```

Replace `_classify` with:
```python
def _ensure_messages(
    session: Session, email: Email, attachments: list[Attachment]
) -> list[EmailMessage]:
    existing = list(
        session.scalars(
            select(EmailMessage).where(EmailMessage.email_id == email.id).order_by(EmailMessage.position)
        )
    )
    if existing:  # reprocess / retry: never re-split, never self-match
        return existing
    parts = split_email(
        body_html=email.body_html, body_text=email.body_text, sender=email.sender_address,
        received_at=email.received_at, client_domains=get_settings().client_domain_list(),
    )
    def no_content(p) -> bool:
        return (p.is_internal and not p.has_payment_signal) or not p.body_text.strip()
    carrier = next((p.position for p in parts if not no_content(p)), 0) if attachments else None
    rows: list[EmailMessage] = []
    for p in parts or []:
        carries = carrier is not None and p.position == carrier
        status, reason, seen_in = "new", None, None
        if not carries and no_content(p):
            status = "no_content"
        elif not carries and p.has_payment_signal:
            match = (
                find_seen_by_fingerprint(session, email_id=email.id, fingerprint=p.fingerprint)
                if p.fingerprint else None
            )
            if match is not None:
                status, reason, seen_in = "seen", "fingerprint", match.id
            elif all_references_recorded(session, find_references(p.body_text)):
                status, reason = "seen", "references_recorded"
        row = EmailMessage(
            email_id=email.id, position=p.position, sender=p.sender, sent_at=p.sent_at,
            raw_header=p.raw_header, is_internal=p.is_internal, carries_attachments=carries,
            body_text=p.body_text if status != "seen" else None,
            tables=p.tables if status != "seen" else None,
            fingerprint=p.fingerprint, status=status, seen_reason=reason,
            seen_in_message_id=seen_in,
        )
        session.add(row)
        rows.append(row)
    if not rows:  # empty body: one row so attachments still have a home
        row = EmailMessage(email_id=email.id, position=0, sender=email.sender_address.lower(),
                           sent_at=email.received_at, raw_header="", is_internal=False,
                           carries_attachments=bool(attachments), body_text="", tables=[],
                           status="new")
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def _classify(session: Session, email: Email, blob_store: BlobStore) -> str:
    atts = list(session.scalars(select(Attachment).where(Attachment.email_id == email.id)))
    messages = _ensure_messages(session, email, atts)
    carrier = next((m for m in messages if m.carries_attachments), None)
    specs: list[tuple[SourceSpec, uuid.UUID | None]] = []
    for spec in classify_email(email, atts, blob_store):
        if spec.ref != "body":  # attachments; the email-level body spec is replaced below
            specs.append((spec, carrier.id if carrier else None))
    live_attachment = any(not s.skipped for s, _ in specs)
    for m in messages:
        if m.status != "new":
            continue
        tables = m.tables or []
        text = m.body_text or ""
        if has_numeric_table_rows(tables):
            specs.append((SourceSpec("body_table", "body"), m.id))
        elif not (m.carries_attachments and live_attachment) and len(
            re.sub(r"\s+", "", text)
        ) >= 120 and any(ch.isdigit() for ch in text):
            specs.append((SourceSpec("body_text", "body"), m.id))
    if not any(not s.skipped for s, _ in specs):
        if any(m.status == "seen" for m in messages) or all(
            m.status in ("seen", "no_content") for m in messages
        ):
            email.status = "done"  # everything here is already recorded or empty
            session.flush()
            return "classified"
        specs.append(
            (SourceSpec("body_text", "body", skipped=True, skip_reason="no extractable content"), None)
        )
    for spec, message_id in specs:
        session.add(
            ExtractionSource(
                email_id=email.id, email_message_id=message_id, kind=spec.kind, ref=spec.ref,
                skipped=spec.skipped, skip_reason=spec.skip_reason,
            )
        )
    email.status = "classified"
    session.flush()
    return "classified"
```
(`import re` at the top of `advance.py`.) The 120-character rule mirrors the classifier's `_BODY_TEXT_MIN_CHARS`; import that constant instead of repeating `120` if you prefer.

In `_run_extractor`, before the existing `if src.ref == "body":` branch add:
```python
    if src.ref == "body" and src.email_message_id is not None:
        msg = session.get(EmailMessage, src.email_message_id)
        if msg is None:
            raise ValueError(f"extraction_source {src.id} references a missing message")
        tables = msg.tables or [] if src.kind == "body_table" else []
        return ExtractedContent(
            text=msg.body_text or "", tables=tables, meta={"email_message_id": str(msg.id)}
        )
```

- [ ] **Step 5: Run tests (new + existing pipeline)**

Run: `uv run pytest tests/threads tests/pipeline tests/classify tests/extract tests/normalize tests/review -q`
Expected: PASS. Existing fixtures (single-message emails) must behave as before: one message, same source kinds. If an existing test relied on the email-level `body_text` source for an email whose text the splitter now attributes differently, compare the produced sources against the old ones and report the difference before changing any test.

- [ ] **Step 6: Commit**
```bash
git add ar_pipeline/threads/memory.py ar_pipeline/pipeline/advance.py ar_pipeline/classify/classifier.py tests/threads
git commit -m "feat(threads): thread memory and per-message sources"
```

---
### Task 6: Payment keys, duplicates and history (DB)

**Files:**
- Create: `ar_pipeline/threads/dedupe.py`
- Test: `tests/threads/test_dedupe.py`

**Interfaces:**
- Consumes: `payment_key_for`, `canonical_total` (Task 3); `KEY_LIVE` (Task 5).
- Produces:
  - `assign_payment_key(session, row: Extraction) -> None` — sets `payment_key`/`payment_key_strength`, or marks `status="duplicate"` + `duplicate_of_id`, or adds a flag; takes `pg_advisory_xact_lock(hashtext(key))` **before** looking up strong keys.
  - `apply_history(session, row: Extraction, message: EmailMessage | None, newest_content_position: int | None) -> None` — sets `historical_reason` and appends its flag.
  - Flag texts (exact):
    - `f"header: possible duplicate of payment {other_id} (same payer, amount and date)"`
    - `f"header: reference {ref} was already used for {amount} (payment {other_id})"`
    - `f"header: rejected before on {date:%d %b %Y} by {who}: {reason}"`
    - `"header: historical — earlier message in this thread; check it isn't already in your books"`
    - `f"header: historical — dated {d:%d %b %Y}, before go-live {g:%d %b %Y}; likely already recorded"`

- [ ] **Step 1: Write the failing tests**

`tests/threads/test_dedupe.py`:
```python
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import ar_pipeline.config as config_module
from ar_pipeline.db.models import Email, EmailMessage, Extraction
from ar_pipeline.threads.dedupe import apply_history, assign_payment_key


def _email(db_session, mid):
    e = Email(internet_message_id=mid, sender_address="a@b.com", sender_domain="b.com",
              subject="s", received_at=dt.datetime(2026, 2, 18, tzinfo=dt.UTC), status="review")
    db_session.add(e)
    db_session.flush()
    return e


def _row(db_session, email, *, ref="HDFC1234567890", rtype="utr", total="100.00",
         status="pending_review", date="2026-02-18", payer="Acme Corp"):
    x = Extraction(email_id=email.id, status=status, is_remittance=True, validation_flags=[],
                   canonical={"header": {"payment_reference": ref, "payment_reference_type": rtype,
                                         "payer_name": payer, "total_paid_amount": total,
                                         "payment_date": date}, "line_items": [{}]})
    db_session.add(x)
    db_session.flush()
    return x


def test_first_strong_key_is_assigned(db_session):
    x = _row(db_session, _email(db_session, "d1"))
    assign_payment_key(db_session, x)
    assert (x.payment_key, x.payment_key_strength) == ("utr:HDFC1234567890", "strong")


def test_same_reference_and_amount_within_one_rupee_is_a_duplicate(db_session):
    a = _row(db_session, _email(db_session, "d2"), total="1000.00")
    assign_payment_key(db_session, a)
    b = _row(db_session, _email(db_session, "d3"), total="1000.49")
    assign_payment_key(db_session, b)
    assert b.status == "duplicate" and b.duplicate_of_id == a.id and b.payment_key is None


def test_same_reference_different_amount_is_flagged_not_dropped(db_session):
    a = _row(db_session, _email(db_session, "d4"), total="1000.00")
    assign_payment_key(db_session, a)
    b = _row(db_session, _email(db_session, "d5"), total="2000.00")
    assign_payment_key(db_session, b)
    assert b.status == "pending_review" and b.payment_key is None and b.duplicate_of_id == a.id
    assert any("was already used for" in f for f in b.validation_flags)


def test_rejected_earlier_is_noted(db_session):
    a = _row(db_session, _email(db_session, "d6"))
    assign_payment_key(db_session, a)
    a.status, a.reviewed_by, a.reject_reason = "rejected", "Asha", "wrong payer"
    a.reviewed_at = dt.datetime(2026, 2, 19, tzinfo=dt.UTC)
    db_session.flush()
    b = _row(db_session, _email(db_session, "d7"))
    assign_payment_key(db_session, b)
    assert b.payment_key == "utr:HDFC1234567890"
    assert any(f.startswith("header: rejected before on 19 Feb 2026 by Asha") for f in b.validation_flags)


def test_weak_keys_flag_but_never_block(db_session):
    a = _row(db_session, _email(db_session, "d8"), ref=None, rtype=None)
    assign_payment_key(db_session, a)
    b = _row(db_session, _email(db_session, "d9"), ref=None, rtype=None)
    assign_payment_key(db_session, b)
    assert b.status == "pending_review" and b.payment_key_strength == "weak"
    assert any("possible duplicate of payment" in f for f in b.validation_flags)


def test_history_earlier_message(db_session):
    e = _email(db_session, "h1")
    m = EmailMessage(email_id=e.id, position=2, raw_header="", is_internal=False,
                     carries_attachments=False, status="new")
    db_session.add(m)
    x = _row(db_session, e)
    apply_history(db_session, x, m, newest_content_position=1)
    assert x.historical_reason == "earlier_message"
    assert x.validation_flags[-1].startswith("header: historical — earlier message")


def test_history_before_go_live(db_session, monkeypatch):
    monkeypatch.setenv("GO_LIVE_DATE", "2026-03-01")
    config_module.get_settings.cache_clear()
    try:
        x = _row(db_session, _email(db_session, "h2"), date="2026-02-18")
        apply_history(db_session, x, None, newest_content_position=None)
        assert x.historical_reason == "before_go_live"
        assert "before go-live 01 Mar 2026" in x.validation_flags[-1]
    finally:
        config_module.get_settings.cache_clear()


def test_newest_message_after_go_live_is_not_historical(db_session):
    x = _row(db_session, _email(db_session, "h3"))
    apply_history(db_session, x, None, newest_content_position=None)
    assert x.historical_reason is None and x.validation_flags == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/threads/test_dedupe.py -q`
Expected: FAIL — `ModuleNotFoundError: ar_pipeline.threads.dedupe`.

- [ ] **Step 3: Implement `threads/dedupe.py`**
```python
"""Payment keys, duplicates and history for freshly normalized extractions (spec §3)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ar_pipeline.config import get_settings
from ar_pipeline.db.models import EmailMessage, Extraction
from ar_pipeline.ledger.money import format_money
from ar_pipeline.threads.memory import KEY_LIVE
from ar_pipeline.threads.references import canonical_total, payment_key_for

DUPLICATE_TOLERANCE = Decimal("1.00")


def _flag(row: Extraction, flag: str) -> None:
    row.validation_flags = list(row.validation_flags or []) + [flag]


def assign_payment_key(session: Session, row: Extraction) -> None:
    found = payment_key_for(row.canonical or {})
    if found is None:
        return
    key, strength = found
    total = canonical_total(row.canonical or {})
    if strength == "weak":
        other = session.scalar(
            select(Extraction).where(
                Extraction.payment_key == key, Extraction.status.in_(KEY_LIVE),
                Extraction.id != row.id,
            )
        )
        row.payment_key, row.payment_key_strength = key, "weak"
        if other is not None:
            _flag(row, f"header: possible duplicate of payment {other.id} "
                       "(same payer, amount and date)")
        session.flush()
        return

    # lock BEFORE looking up, so two simultaneous arrivals can't both pass
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
    other = session.scalar(
        select(Extraction).where(
            Extraction.payment_key == key, Extraction.payment_key_strength == "strong",
            Extraction.status.in_(KEY_LIVE), Extraction.id != row.id,
        )
    )
    if other is None:
        rejected = session.scalar(
            select(Extraction)
            .where(Extraction.payment_key == key, Extraction.status == "rejected")
            .order_by(Extraction.reviewed_at.desc().nulls_last())
        )
        row.payment_key, row.payment_key_strength = key, "strong"
        if rejected is not None and rejected.reviewed_at is not None:
            _flag(row, f"header: rejected before on {rejected.reviewed_at:%d %b %Y} by "
                       f"{rejected.reviewed_by}: {rejected.reject_reason}")
        session.flush()
        return

    other_total = canonical_total(other.canonical or {})
    row.duplicate_of_id = other.id
    if total is not None and other_total is not None and abs(total - other_total) <= DUPLICATE_TOLERANCE:
        row.status = "duplicate"
    else:
        currency = ((other.canonical or {}).get("header") or {}).get("currency") or "INR"
        shown = format_money(other_total, currency) if other_total is not None else "another amount"
        _flag(row, f"header: reference {key.split(':', 1)[1]} was already used for {shown} "
                   f"(payment {other.id})")
    session.flush()


def apply_history(
    session: Session, row: Extraction, message: EmailMessage | None,
    newest_content_position: int | None,
) -> None:
    if (
        message is not None and newest_content_position is not None
        and message.position > newest_content_position
    ):
        row.historical_reason = "earlier_message"
        _flag(row, "header: historical — earlier message in this thread; "
                   "check it isn't already in your books")
        session.flush()
        return
    go_live = get_settings().go_live_date
    if go_live is None:
        return
    raw = ((row.canonical or {}).get("header") or {}).get("payment_date")
    when: date | None = None
    try:
        when = date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        when = None
    if when is None and message is not None and message.sent_at is not None:
        when = message.sent_at.date()
    if when is not None and when < go_live:
        row.historical_reason = "before_go_live"
        _flag(row, f"header: historical — dated {when:%d %b %Y}, before go-live "
                   f"{go_live:%d %b %Y}; likely already recorded")
        session.flush()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/threads/test_dedupe.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/threads/dedupe.py tests/threads/test_dedupe.py
git commit -m "feat(threads): payment-key duplicates and history flags"
```

---

### Task 7: Normalize per message

**Files:**
- Modify: `ar_pipeline/normalize/service.py`, `ar_pipeline/normalize/prompt.py`
- Test: `tests/threads/test_normalize_threads.py`

**Interfaces:**
- Consumes: Tasks 5–6; `normalize_email`; `check_against_ledger`; `approve_and_queue`, `settle_email`.
- Produces: `normalize_one(session, email, llm_client) -> int` (same signature) now: groups raw extractions by `email_message_id`; processes `new` messages lacking a live extraction, **oldest first** (highest position first; legacy `None` group first); one `normalize_email` call per group; per-message failure → message `failed` (siblings continue); new rows carry `email_message_id`; every remittance row → `assign_payment_key` → (unless `duplicate`) `apply_history` → ledger flags → auto-approve rule; truncation flag; email `done` via `settle_email`, or `error` when every message failed.
- `prompt.is_truncated(sender_address, subject, raw_extractions) -> bool`.

- [ ] **Step 1: Write the failing tests**

`tests/threads/test_normalize_threads.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

import ar_pipeline.config as config_module
from ar_pipeline.db.models import Email, EmailMessage, Extraction
from ar_pipeline.normalize.llm_client import LLMError
from ar_pipeline.normalize.normalizer import NormalizerOutput, PaymentDraft
from ar_pipeline.pipeline.advance import advance_once
from ar_pipeline.schema.canonical import LineItem
from ar_pipeline.storage import LocalBlobStore
from tests.extract.vision_fake import FakeVisionExtractor
from tests.normalize.llm_fake import FakeLLMClient
from tests.threads.chains import chain_html


@pytest.fixture
def settings_env(monkeypatch):
    monkeypatch.setenv("CLIENT_DOMAINS", "adityabirla.com")
    monkeypatch.setenv("AUTO_APPROVE_MIN_CONFIDENCE", "0.75")
    config_module.get_settings.cache_clear()
    yield
    config_module.get_settings.cache_clear()


def _draft(n: int) -> PaymentDraft:
    amount = Decimal((n + 1) * 1000)
    return PaymentDraft(
        payer_name="Alufluoride Limited", payment_reference=f"PUNBR52026{n:011d}",
        payment_reference_type="utr", total_paid_amount=amount,
        line_items=[LineItem(invoice_number=f"JHMUR25100{n:05d}", invoice_amount=amount,
                             amount_paid=amount)],
        confidence=0.95,
    )


def _out(n: int) -> NormalizerOutput:
    return NormalizerOutput(is_remittance=True, payments=[_draft(n)])


def _ingest(db_session, html, mid):
    e = Email(internet_message_id=mid, sender_address="dharmendra.p.kumar-c@adityabirla.com",
              sender_domain="adityabirla.com", subject="FW: Payment Remittance details",
              received_at=datetime(2026, 2, 18, 12, 8, tzinfo=UTC), body_html=html, status="new")
    db_session.add(e)
    db_session.flush()
    return e


def _run(db_session, tmp_path, llm):
    store = LocalBlobStore(str(tmp_path))
    for _ in range(3):
        advance_once(db_session, store, FakeVisionExtractor(), llm)


def _rows(db_session, email):
    return db_session.scalars(
        select(Extraction).where(Extraction.email_id == email.id).order_by(Extraction.created_at)
    ).all()


def test_first_sight_processes_every_message_oldest_first(db_session, tmp_path, settings_env):
    e = _ingest(db_session, chain_html([3, 2, 1]), "t1")
    llm = FakeLLMClient(responses=[_out(1), _out(2), _out(3)])  # oldest first
    _run(db_session, tmp_path, llm)
    rows = _rows(db_session, e)
    assert len(llm.calls) == 3 and len(rows) == 3
    by_ref = {r.canonical["header"]["payment_reference"]: r for r in rows}
    newest = by_ref["PUNBR52026" + "3".zfill(11)]
    assert newest.status == "approved" and newest.historical_reason is None
    for n in (1, 2):
        older = by_ref["PUNBR52026" + str(n).zfill(11)]
        assert older.status == "pending_review" and older.historical_reason == "earlier_message"


def test_reforward_reads_only_the_new_message(db_session, tmp_path, settings_env):
    _ingest(db_session, chain_html([2, 1]), "t2a")
    _run(db_session, tmp_path, FakeLLMClient(responses=[_out(1), _out(2)]))
    e2 = _ingest(db_session, chain_html([3, 2, 1]), "t2b")
    llm = FakeLLMClient(responses=[_out(3)])
    _run(db_session, tmp_path, llm)
    assert len(llm.calls) == 1
    msgs = db_session.scalars(
        select(EmailMessage).where(EmailMessage.email_id == e2.id).order_by(EmailMessage.position)
    ).all()
    assert [m.status for m in msgs] == ["no_content", "new", "seen", "seen"]
    assert {m.seen_reason for m in msgs[2:]} == {"fingerprint"}


def test_duplicate_payment_from_a_mangled_copy(db_session, tmp_path, settings_env):
    _ingest(db_session, chain_html([1]), "t3a")
    _run(db_session, tmp_path, FakeLLMClient(responses=[_out(1)]))
    # same payment, different wording: fingerprint misses, reference check catches it
    html = chain_html([1]).replace("Dear Dharmendra Ji,", "Hi team, see corrected below.")
    e2 = _ingest(db_session, html, "t3b")
    llm = FakeLLMClient(responses=[_out(1)])
    _run(db_session, tmp_path, llm)
    assert len(llm.calls) == 0
    assert db_session.get(Email, e2.id).status == "done"


def test_one_failed_message_does_not_block_its_siblings(db_session, tmp_path, settings_env):
    e = _ingest(db_session, chain_html([2, 1]), "t4")
    _run(db_session, tmp_path, FakeLLMClient(responses=[LLMError("boom"), _out(2)]))
    msgs = db_session.scalars(
        select(EmailMessage).where(EmailMessage.email_id == e.id).order_by(EmailMessage.position)
    ).all()
    assert [m.status for m in msgs] == ["no_content", "new", "failed"]
    assert "boom" in (msgs[2].error_detail or "")
    assert len(_rows(db_session, e)) == 1
```
Payload `_out(n)` order: messages are processed oldest first, i.e. payment 1 (position 3) before payment 3 (position 1). The duplicate test exercises the reference check (`UTR no.PUNBR…` appears in the text). If `find_references` normalization yields a different string than the drafted `payment_reference`, align the fixture (both must be the same uppercase alphanumerics).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/threads/test_normalize_threads.py -q`
Expected: FAIL (one AI call for the whole email; no historical reasons; no message statuses).

- [ ] **Step 3: `prompt.is_truncated`**

In `prompt.py`, split `build_user_message` into `_render(sender_address, subject, raw_extractions) -> str` (everything up to, not including, the length cap) and keep `build_user_message` as `_render` + the existing cap/log. Add:
```python
def is_truncated(sender_address: str, subject: str, raw_extractions: list[dict]) -> bool:
    return len(_render(sender_address, subject, raw_extractions)) > _MAX_USER_CHARS
```

- [ ] **Step 4: Rewrite `normalize_one`**

Replace the body of `ar_pipeline/normalize/service.py` (keep the module docstring, update it to say "per message") with:
```python
from __future__ import annotations

import uuid
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ar_pipeline.config import get_settings
from ar_pipeline.db.models import Email, EmailMessage, Extraction, ExtractionSource, RawExtraction
from ar_pipeline.ledger.checks import check_against_ledger
from ar_pipeline.normalize.llm_client import LLMClient
from ar_pipeline.normalize.normalizer import normalize_email
from ar_pipeline.normalize.prompt import PROMPT_VERSION, is_truncated
from ar_pipeline.pipeline.routing import AUTO_REVIEWER, approve_and_queue, settle_email
from ar_pipeline.schema.canonical import RemittancePayload
from ar_pipeline.threads.dedupe import apply_history, assign_payment_key

__all__ = ["normalize_one"]

TRUNCATED_FLAG = "header: content was truncated — check nothing is missing"
_LIVE = ("pending_review", "approved", "rejected", "already_recorded", "duplicate")
_LEGACY_ORDER = 10**6  # emails classified before threads: whole email, processed first


def _groups(session: Session, email: Email) -> list[tuple[EmailMessage | None, list[dict]]]:
    payloads: dict[uuid.UUID | None, list[dict]] = defaultdict(list)
    for message_id, payload in session.execute(
        select(ExtractionSource.email_message_id, RawExtraction.payload)
        .join(RawExtraction, RawExtraction.extraction_source_id == ExtractionSource.id)
        .where(ExtractionSource.email_id == email.id)
        .order_by(ExtractionSource.kind, ExtractionSource.ref, RawExtraction.id)
    ):
        payloads[message_id].append(payload)
    messages = {
        m.id: m for m in session.scalars(select(EmailMessage).where(EmailMessage.email_id == email.id))
    }
    out: list[tuple[EmailMessage | None, list[dict]]] = []
    for message_id, raws in payloads.items():
        message = messages.get(message_id) if message_id else None
        if message is not None and message.status != "new":
            continue
        live = select(func.count()).select_from(Extraction).where(
            Extraction.email_id == email.id, Extraction.status.in_(_LIVE),
            Extraction.email_message_id.is_(None) if message_id is None
            else Extraction.email_message_id == message_id,
        )
        if session.scalar(live):
            continue
        out.append((message, raws))
    out.sort(key=lambda g: -(g[0].position if g[0] is not None else _LEGACY_ORDER))
    return out


def _newest_content_position(session: Session, email: Email) -> int | None:
    return session.scalar(
        select(func.min(EmailMessage.position)).where(
            EmailMessage.email_id == email.id, EmailMessage.status.in_(("new", "seen", "failed"))
        )
    )


def _normalize_group(
    session: Session, email: Email, message: EmailMessage | None, raws: list[dict],
    llm_client: LLMClient,
) -> list[Extraction]:
    sender = (message.sender if message is not None and message.sender and "@" in message.sender
              else email.sender_address)
    model = get_settings().llm_model
    out, payments = normalize_email(
        email_id=str(email.id), sender_address=sender, subject=email.subject,
        raw_extractions=raws, llm_client=llm_client,
    )
    truncated = is_truncated(sender, email.subject, raws)
    message_id = message.id if message is not None else None
    rows: list[Extraction] = []
    if payments:
        for payment in payments:
            flags = list(payment.validation_flags) + ([TRUNCATED_FLAG] if truncated else [])
            row = Extraction(
                email_id=email.id, email_message_id=message_id,
                canonical=payment.payload.model_dump(mode="json"), confidence=payment.confidence,
                is_remittance=payment.is_remittance, validation_flags=flags, llm_model=model,
                prompt_version=PROMPT_VERSION, raw_llm_response=dict(payment.raw_llm_response),
                status="pending_review",
            )
            session.add(row)
            rows.append(row)
    else:
        if not out.is_remittance:
            flags = ["LLM: not a remittance"]
        elif out.notes:
            flags = [f"LLM: {out.notes}"]
        else:
            flags = ["LLM returned no payments"]
        session.add(
            Extraction(
                email_id=email.id, email_message_id=message_id, canonical={},
                confidence=Decimal("0"), is_remittance=out.is_remittance, validation_flags=flags,
                llm_model=model, prompt_version=PROMPT_VERSION,
                raw_llm_response=out.model_dump(mode="json"), status="pending_review",
            )
        )
    session.flush()
    for row in rows:
        env = row.canonical.get("envelope")
        if isinstance(env, dict):
            row.canonical["envelope"] = {**env, "extraction_id": str(row.id)}
    session.flush()
    return rows


def _route(session: Session, email: Email, message: EmailMessage | None, rows: list[Extraction],
           newest: int | None) -> None:
    threshold = get_settings().auto_approve_min_confidence
    for row in rows:
        if not (row.is_remittance and row.canonical):
            continue
        assign_payment_key(session, row)
        if row.status == "duplicate":
            continue
        apply_history(session, row, message, newest)
        payload = RemittancePayload.model_validate(row.canonical)
        row.validation_flags = list(row.validation_flags) + check_against_ledger(session, payload)
        session.flush()
        if (
            threshold > 0 and not row.validation_flags and row.confidence is not None
            and float(row.confidence) >= threshold
        ):
            approve_and_queue(session, row, reviewed_by=AUTO_REVIEWER)


def normalize_one(session: Session, email: Email, llm_client: LLMClient) -> int:
    """Normalize every new, not-yet-processed message of one email (oldest first)."""
    groups = _groups(session, email)
    if not groups:
        has_rows = session.scalar(
            select(func.count()).select_from(Extraction).where(
                Extraction.email_id == email.id, Extraction.status != "superseded"
            )
        )
        if has_rows:
            email.status = "review"
            settle_email(session, email)
            return 0
        email.status = "error"
        email.error_detail = "no raw extractions to normalize"
        session.flush()
        return 0

    newest = _newest_content_position(session, email)
    added = failed = 0
    for message, raws in groups:
        if message is None:  # legacy whole-email path keeps whole-email failure
            rows = _normalize_group(session, email, None, raws, llm_client)
            _route(session, email, None, rows, newest)
            added += max(len(rows), 1)
            continue
        try:
            with session.begin_nested():
                rows = _normalize_group(session, email, message, raws, llm_client)
                _route(session, email, message, rows, newest)
            added += max(len(rows), 1)
        except Exception as exc:  # noqa: BLE001 -- per-message isolation is the point
            message.status = "failed"
            message.error_detail = f"{type(exc).__name__}: {exc}"[:2000]
            failed += 1
            session.flush()

    if added == 0 and failed:
        email.status = "error"
        email.error_detail = f"{failed} message(s) failed — see Errors"
        session.flush()
        return 0
    email.status = "review"
    session.flush()
    settle_email(session, email)
    return added
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/threads tests/normalize tests/pipeline tests/review tests/ledger -q`
Expected: PASS. Existing tests asserting exact flag lists may now also see payment-key flags (e.g. two fixtures sharing a UTR) — investigate each such change; it must be explained by Stage 1 behaviour before a test is updated.

- [ ] **Step 6: Commit**
```bash
git add ar_pipeline/normalize tests/threads/test_normalize_threads.py
git commit -m "feat(threads): normalize per message, oldest first, with failure isolation"
```

---

### Task 8: "Already recorded" and key upkeep on edits

**Files:**
- Modify: `ar_pipeline/review/service.py`, `ar_pipeline/review/app.py`, `ar_pipeline/review/templates/_queue_rows.html`, `ar_pipeline/review/templates/detail.html`
- Test: `tests/review/test_already_recorded.py`

**Interfaces — Produces:**
- `mark_already_recorded(session, extraction_ids: list[uuid.UUID], user: User) -> int` — only `pending_review`, remittance, non-empty canonical, `historical_reason` not null; sets `already_recorded`, `reviewed_by/at`, `envelope.reviewed_by`; `post_extraction` (ledger) but **no** `Delivery`; `settle_email`.
- `POST /review/bulk-already-recorded` (form: `extraction_id` list) → `/review/queue?flash=N marked already recorded`; `POST /review/{id}/already-recorded` → next item via `_to_next_item`.
- `QueueRow.historical_reason: str | None`.
- `save_edits` recomputes the payment key when the header reference/type/payer/date/total changed: clear the row's key, call `assign_payment_key`; if that marks it `duplicate` or flags a conflicting amount on a strong key, raise `ReviewError(f"that reference already belongs to payment {other}")` instead (edits are rolled back by the caller's error path — keep the row unchanged).
- `_extraction_outcome`: `already_recorded` → `"already recorded"`, `duplicate` → `"duplicate"`.

- [ ] **Step 1: Write the failing tests**

`tests/review/test_already_recorded.py`:
```python
from __future__ import annotations

from sqlalchemy import select

from ar_pipeline.db.models import Delivery, Extraction, InvoicePayment


def _historical(db_session, seed_pending):
    _e, ext = seed_pending()
    ext.historical_reason = "earlier_message"
    ext.validation_flags = ["header: historical — earlier message in this thread; check it isn't already in your books"]
    db_session.flush()
    return ext


def test_bulk_mark_already_recorded_posts_to_ledger_without_delivery(client, db_session, seed_pending):
    ext = _historical(db_session, seed_pending)
    _e2, current = seed_pending()  # not historical: must be ignored
    r = client.post("/review/bulk-already-recorded",
                    data={"extraction_id": [str(ext.id), str(current.id)]})
    assert r.status_code == 303 and "1 marked already recorded" in r.headers["location"].replace("%20", " ")
    db_session.expire_all()
    assert db_session.get(Extraction, ext.id).status == "already_recorded"
    assert db_session.get(Extraction, current.id).status == "pending_review"
    assert not db_session.scalars(select(Delivery).where(Delivery.extraction_id == ext.id)).all()
    assert db_session.scalars(select(InvoicePayment).where(InvoicePayment.extraction_id == ext.id)).all()


def test_queue_offers_already_recorded_only_for_historical(client, db_session, seed_pending):
    ext = _historical(db_session, seed_pending)
    _e2, current = seed_pending()
    text = client.get("/review/queue").text
    assert "/review/bulk-already-recorded" in text
    assert f'form="bulkRecordedForm" value="{ext.id}"' in text or f'value="{ext.id}" form="bulkRecordedForm"' in text
    assert f'value="{current.id}" form="bulkRecordedForm"' not in text


def test_detail_button_for_historical(client, db_session, seed_pending):
    ext = _historical(db_session, seed_pending)
    page = client.get(f"/review/{ext.id}").text
    assert f"/review/{ext.id}/already-recorded" in page
    r = client.post(f"/review/{ext.id}/already-recorded")
    assert r.status_code == 303
    db_session.expire_all()
    assert db_session.get(Extraction, ext.id).status == "already_recorded"


def test_editing_a_reference_onto_an_existing_payment_is_refused(client, db_session, seed_pending):
    _e1, first = seed_pending()
    first.payment_key, first.payment_key_strength, first.status = "utr:UTR1", "strong", "approved"
    _e2, second = seed_pending()
    db_session.flush()
    form = {"header.payer_name": "Acme Corp", "header.currency": "INR",
            "header.total_paid_amount": "90.00", "header.payment_reference": "UTR1",
            "header.payment_reference_type": "utr",
            "line_items[0].invoice_number": "INV-1", "line_items[0].invoice_amount": "100.00",
            "line_items[0].amount_paid": "90.00", "line_items[0].deductions[0].type": "tds",
            "line_items[0].deductions[0].amount": "10.00"}
    r = client.post(f"/review/{second.id}/edit", data=form)
    assert "already belongs to payment" in r.headers["location"].replace("%20", " ")
```
(`seed_pending`'s canonical uses `payment_reference: "UTR-1"` — normalized `UTR1` — so the second seeded row would collide once keyed; the first test seeds rows without keys, as `seed_pending` sets none.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/review/test_already_recorded.py -q`
Expected: FAIL — routes 404/405.

- [ ] **Step 3: Implement**

`service.py`:
```python
def mark_already_recorded(session: Session, extraction_ids: list[uuid.UUID], user: User) -> int:
    """Historical payments the client already has in their books: post them to the
    invoice ledger (balances stay true) but never deliver them (no double-posting)."""
    from ar_pipeline.ledger.posting import post_extraction

    done = 0
    for extraction_id in extraction_ids:
        ext = session.get(Extraction, extraction_id)
        if (ext is None or ext.status != "pending_review" or not ext.is_remittance
                or not ext.canonical or ext.historical_reason is None):
            continue
        ext.status = "already_recorded"
        ext.reviewed_by = user.name
        ext.reviewed_at = func.now()
        env = ext.canonical.get("envelope")
        if isinstance(env, dict):
            ext.canonical["envelope"] = {**env, "reviewed_by": user.name}
        session.flush()
        post_extraction(session, ext)
        email = session.get(Email, ext.email_id)
        assert email is not None
        settle_email(session, email)
        done += 1
    return done
```
Add `historical_reason=ext.historical_reason` to `QueueRow` construction (new field), and the two outcome labels in `_extraction_outcome`.

In `save_edits`, after `ext.canonical = normalised` and before the flags are recomputed:
```python
    key_fields = ("payment_reference", "payment_reference_type", "payer_name",
                  "payment_date", "total_paid_amount")
    old_header, new_header = stored.get("header", {}) or {}, normalised["header"]
    if any(old_header.get(f) != new_header.get(f) for f in key_fields):
        from ar_pipeline.threads.dedupe import assign_payment_key

        ext.payment_key = ext.payment_key_strength = None
        ext.duplicate_of_id = None
        session.flush()
        assign_payment_key(session, ext)
        if ext.status == "duplicate" or ext.duplicate_of_id is not None:
            raise ReviewError(f"that reference already belongs to payment {ext.duplicate_of_id}")
```
`edit_action` already turns `ReviewError` into a flash redirect. Because `save_edits` has already flushed edit rows before raising, wrap this check **before** writing `ExtractionEdit` rows, or make `edit_action` roll back the nested work: the simplest correct order is to compute the key on a copy first. Implement the check before the `for path, old, new in edits:` loop by building `normalised` first (it already is), then running the key logic, and only then adding `ExtractionEdit` rows and assigning `ext.canonical`.

`app.py` — declare before the `/{extraction_id}` routes (next to `/bulk-reject`):
```python
@router.post("/bulk-already-recorded")
def bulk_already_recorded_action(
    extraction_id: list[str] = Form(default=[]),
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.review.service import mark_already_recorded

    ids = []
    for raw in extraction_id:
        try:
            ids.append(uuid.UUID(raw))
        except ValueError:
            continue
    n = mark_already_recorded(session, ids, user)
    return RedirectResponse(
        f"/review/queue?flash={quote(f'{n} marked already recorded')}", status_code=303
    )
```
and, before `/{extraction_id}/reprocess`:
```python
@router.post("/{extraction_id}/already-recorded")
def already_recorded_action(
    extraction_id: uuid.UUID,
    user: User = Depends(require_user),
    session: Session = Depends(get_db),
) -> Response:
    from ar_pipeline.review.service import mark_already_recorded, next_pending_after

    planned = next_pending_after(session, extraction_id)
    if mark_already_recorded(session, [extraction_id], user) == 0:
        flash = quote("Only historical payments can be marked already recorded")
        return RedirectResponse(f"/review/{extraction_id}?flash={flash}", status_code=303)
    return _to_next_item(session, planned, "Marked already recorded")
```

`_queue_rows.html` — add a second bar and checkbox column for historical rows, mirroring the bulk-reject bar:
```html
{% set history = rows|selectattr("historical_reason")|list %}
{% if history %}
<form method="post" action="/review/bulk-already-recorded" id="bulkRecordedForm" class="bulk-bar">
  <button type="submit">Mark selected as already recorded</button>
  <span class="bulk-note">Only historical payments (earlier messages or before go-live) can be marked. They are added to the invoice ledger but never sent to your system again.</span>
</form>
{% endif %}
```
and in each row's select cell, after the not-a-remittance checkbox:
```html
{% if r.historical_reason %}<input type="checkbox" name="extraction_id" value="{{ r.extraction_id }}" form="bulkRecordedForm" aria-label="Mark {{ r.subject }} already recorded">{% endif %}
```
Show the select column when `junk or history`.

`detail.html` — next to the Reject form, for `view.extraction.historical_reason`:
```html
{% if view.extraction.historical_reason %}
<form method="post" action="/review/{{ view.extraction.id }}/already-recorded" class="stack">
  <button type="submit" class="btn-quiet">Already recorded in our books</button>
</form>
{% endif %}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/review tests/ledger tests/threads -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/review tests/review/test_already_recorded.py
git commit -m "feat(threads): mark historical payments already recorded"
```

---

### Task 9: Review UI — thread view, badges, failed messages

**Files:**
- Modify: `ar_pipeline/review/service.py`, `app.py`, `templates/email.html`, `templates/detail.html`, `templates/_journey_rows.html`, `templates/errors.html`, `static/review.css`
- Test: `tests/review/test_thread_views.py`

**Interfaces — Produces:**
- `EmailView.messages: list[EmailMessage]` (ordered by position).
- `DetailView.message: EmailMessage | None` (the extraction's source message).
- `JourneyRow.failed_message_count: int`, `seen_message_count: int`.
- `list_failed_messages(session) -> list[tuple[EmailMessage, Email]]`; `retry_message(session, message_id)` → message `new`, `error_detail` None, email status `extracted` (raises `ReviewError` unless status `failed`).
- Route `POST /review/messages/{message_id}/retry` (before `/{extraction_id}` routes) → `/review/errors?flash=Retrying`.

- [ ] **Step 1: Write the failing tests**

`tests/review/test_thread_views.py`:
```python
from __future__ import annotations

from ar_pipeline.db.models import EmailMessage


def _msg(db_session, email, position, status, **kw):
    m = EmailMessage(email_id=email.id, position=position, raw_header="From: x\nSent: y",
                     is_internal=False, carries_attachments=False, status=status,
                     sender="str@alufluoride.com", body_text="We have remitted Rs.1,000.00", **kw)
    db_session.add(m)
    db_session.flush()
    return m


def test_email_view_lists_thread_messages(client, db_session, seed_pending):
    email, _ext = seed_pending()
    _msg(db_session, email, 0, "no_content")
    _msg(db_session, email, 1, "new")
    _msg(db_session, email, 2, "seen", seen_reason="fingerprint")
    text = client.get(f"/review/email/{email.id}").text
    assert "Messages in this thread" in text
    assert "internal forward" in text and "seen before" in text


def test_detail_shows_source_message_and_historical_badge(client, db_session, seed_pending):
    email, ext = seed_pending()
    m = _msg(db_session, email, 1, "new")
    ext.email_message_id = m.id
    ext.historical_reason = "earlier_message"
    db_session.flush()
    page = client.get(f"/review/{ext.id}").text
    assert "We have remitted Rs.1,000.00" in page
    assert "Historical: earlier message" in page
    assert f"/review/email/{email.id}" in page


def test_failed_message_is_listed_and_retryable(client, db_session, seed_pending):
    email, _ext = seed_pending()
    m = _msg(db_session, email, 1, "failed", error_detail="LLMError: boom")
    page = client.get("/review/errors").text
    assert "Failed messages" in page and "boom" in page
    r = client.post(f"/review/messages/{m.id}/retry")
    assert r.status_code == 303
    db_session.refresh(m)
    assert m.status == "new"


def test_journey_counts_failed_and_seen(client, db_session, seed_pending):
    email, _ext = seed_pending()
    _msg(db_session, email, 1, "failed", error_detail="x")
    _msg(db_session, email, 2, "seen", seen_reason="fingerprint")
    text = client.get("/review/journey-rows").text
    assert "1 message failed" in text and "1 seen before" in text
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/review/test_thread_views.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**
- `load_email_view`: load `messages = list(session.scalars(select(EmailMessage).where(EmailMessage.email_id == email.id).order_by(EmailMessage.position)))` and pass it into `EmailView(..., messages=messages)` (new field, last).
- `load_detail`: `message = session.get(EmailMessage, ext.email_message_id) if ext.email_message_id else None`; add `message` as the last `DetailView` field.
- `list_journey`: two group-by counts like `skipped_count_by_email` (status `failed`, status `seen`) → `JourneyRow.failed_message_count`, `seen_message_count`.
- `list_failed_messages` / `retry_message` as specified.

`email.html` — after the extractions table:
```html
{% if view.messages|length > 1 %}
<h2>Messages in this thread</h2>
<div class="table-wrap">
<table class="subtable">
  <thead><tr><th>#</th><th>From</th><th>Sent</th><th>Status</th></tr></thead>
  <tbody>
  {% for m in view.messages %}
  <tr>
    <td>{{ m.position }}</td>
    <td class="truncate" title="{{ m.sender or '' }}">{{ m.sender or "—" }}</td>
    <td>{{ m.sent_at.strftime("%Y-%m-%d %H:%M") if m.sent_at else "—" }}</td>
    <td>
      {% if m.status == "new" %}<span class="badge ok">read</span>
      {% elif m.status == "seen" %}<span class="badge">seen before{% if m.seen_reason == "references_recorded" %} (references already recorded){% endif %}</span>
      {% elif m.status == "no_content" %}<span class="badge">internal forward</span>
      {% else %}<span class="badge bad">failed</span>{% endif %}
    </td>
  </tr>
  {% endfor %}
  </tbody>
</table>
</div>
{% endif %}
```
`detail.html` — in the "Original" pane, when `view.message` is set, show its text instead of the whole body:
```html
{% if view.message %}
  <p class="lede">This payment came from message {{ view.message.position }} of the thread
    ({{ view.message.sender or "unknown sender" }}). <a href="/review/email/{{ view.email.id }}">Show full thread</a></p>
  <pre class="raw">{{ view.message.raw_header }}{{ view.message.body_text or "" }}</pre>
{% elif safe_body_html %} … existing iframe … {% else %} … existing pre … {% endif %}
```
and under the flags line:
```html
{% if view.extraction.historical_reason == "earlier_message" %}<span class="badge warn">Historical: earlier message</span>{% elif view.extraction.historical_reason == "before_go_live" %}<span class="badge warn">Historical: before go-live</span>{% endif %}
{% if view.extraction.duplicate_of_id %}<a class="badge" href="/review/extraction/{{ view.extraction.duplicate_of_id }}">Linked to payment {{ view.extraction.duplicate_of_id|string|truncate(8, True, "") }}…</a>{% endif %}
```
`_journey_rows.html` — next to the skipped-attachments note:
```html
{% if r.failed_message_count %}<span class="skipped-note">{{ r.failed_message_count }} message{{ "s" if r.failed_message_count != 1 else "" }} failed</span>{% endif %}
{% if r.seen_message_count %}<span class="seen-note">{{ r.seen_message_count }} seen before</span>{% endif %}
```
`errors.html` — a "Failed messages" table (received, subject, message #, error, Retry form posting to `/review/messages/{{ m.id }}/retry`). The errors route passes `failed_messages=list_failed_messages(session)`.
CSS: `.cell-stack .seen-note { color: var(--text-muted); font-size: .8rem; }`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/review -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/review tests/review/test_thread_views.py
git commit -m "feat(threads): thread view, history badges, failed-message retry"
```

---

### Task 10: Prompt caching

**Files:**
- Modify: `ar_pipeline/normalize/llm_client.py`
- Test: `tests/normalize/test_llm_cache.py`

**Interfaces — Produces:** `AnthropicLLMClient.parse` sends `system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]` and logs `usage.cache_read_input_tokens` / `cache_creation_input_tokens` at DEBUG.

- [ ] **Step 1: Write the failing test**

`tests/normalize/test_llm_cache.py`:
```python
from __future__ import annotations

from types import SimpleNamespace

from pydantic import BaseModel

from ar_pipeline.normalize.llm_client import AnthropicLLMClient


class _Out(BaseModel):
    ok: bool


class _Messages:
    def __init__(self):
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            stop_reason="end_turn", parsed_output=_Out(ok=True),
            usage=SimpleNamespace(cache_read_input_tokens=900, cache_creation_input_tokens=0),
        )


def test_system_prompt_is_sent_as_a_cached_block():
    messages = _Messages()
    client = AnthropicLLMClient(client=SimpleNamespace(messages=messages))  # type: ignore[arg-type]
    assert client.parse(system="SYSTEM", user="u", output_model=_Out).ok
    assert messages.kwargs["system"] == [
        {"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}
    ]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/normalize/test_llm_cache.py -q`
Expected: FAIL — `system` is a plain string.

- [ ] **Step 3: Implement**

In `AnthropicLLMClient.parse` change `system=system,` to:
```python
                # one AI call per thread message: the identical instructions are
                # cached, so calls 2..N of a thread read them at ~0.1x the price.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
```
After the stop-reason checks:
```python
        usage = getattr(response, "usage", None)
        if usage is not None:
            log.debug(
                "llm cache: read=%s created=%s",
                getattr(usage, "cache_read_input_tokens", None),
                getattr(usage, "cache_creation_input_tokens", None),
            )
```
(add `import logging` / `log = logging.getLogger(__name__)` if the module has no logger.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/normalize -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/normalize/llm_client.py tests/normalize/test_llm_cache.py
git commit -m "feat(threads): cache the system prompt across per-message AI calls"
```

---

### Task 11: Backfill command, settings docs, end-to-end verification

**Files:**
- Modify: `ar_pipeline/cli.py`, `.env.example`, `README.md`
- Create: `ar_pipeline/threads/backfill.py`
- Test: `tests/threads/test_backfill.py`, `tests/test_cli.py` (append)

**Interfaces — Produces:** `threads.backfill.backfill(session) -> tuple[int, int, int]` = (messages created, keys assigned, conflicts flagged); CLI `ar-pipeline threads-backfill` prints `"{m} message row(s) created, {k} payment key(s) assigned, {c} conflict(s) flagged"`.

- [ ] **Step 1: Write the failing tests**

`tests/threads/test_backfill.py`:
```python
from __future__ import annotations

from sqlalchemy import select

from ar_pipeline.db.models import EmailMessage, Extraction, ExtractionSource
from ar_pipeline.threads.backfill import backfill


def test_backfill_creates_messages_and_keys_and_flags_conflicts(db_session, seed_pending):
    e1, x1 = seed_pending()
    e2, x2 = seed_pending()  # same canonical -> same UTR "UTR-1"
    x1.status = "approved"
    db_session.add(ExtractionSource(email_id=e1.id, kind="body_text", ref="body"))
    db_session.flush()
    created, keyed, conflicts = backfill(db_session)
    assert (created, keyed, conflicts) == (2, 1, 1)
    m1 = db_session.scalar(select(EmailMessage).where(EmailMessage.email_id == e1.id))
    assert m1.position == 0 and m1.status == "new"
    assert db_session.scalar(
        select(ExtractionSource.email_message_id).where(ExtractionSource.email_id == e1.id)
    ) == m1.id
    db_session.refresh(x1)
    db_session.refresh(x2)
    assert x1.payment_key == "utr:UTR1" and x2.payment_key is None
    assert any("possible duplicate" in f for f in x2.validation_flags)
    assert backfill(db_session) == (0, 0, 0)
```
Append to `tests/test_cli.py`:
```python
def test_threads_backfill_command(wired, capsys, monkeypatch):
    monkeypatch.setattr("ar_pipeline.threads.backfill.backfill", lambda session: (3, 2, 1))
    from ar_pipeline.cli import main

    assert main(["threads-backfill"]) == 0
    assert "3 message row(s) created, 2 payment key(s) assigned, 1 conflict(s) flagged" in capsys.readouterr().out
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/threads/test_backfill.py tests/test_cli.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`ar_pipeline/threads/backfill.py`:
```python
"""Bring rows created before threads up to date (run once after migration 0005)."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Email, EmailMessage, Extraction, ExtractionSource
from ar_pipeline.threads.memory import KEY_LIVE
from ar_pipeline.threads.references import payment_key_for


def backfill(session: Session) -> tuple[int, int, int]:
    created = keyed = conflicts = 0
    have = set(session.scalars(select(EmailMessage.email_id).distinct()))
    for email in session.scalars(select(Email).order_by(Email.received_at.asc())):
        if email.id in have:
            continue
        msg = EmailMessage(email_id=email.id, position=0, sender=email.sender_address.lower(),
                           sent_at=email.received_at, raw_header="", is_internal=False,
                           carries_attachments=True, status="new")
        session.add(msg)
        session.flush()
        session.execute(update(ExtractionSource).where(ExtractionSource.email_id == email.id)
                        .values(email_message_id=msg.id))
        session.execute(update(Extraction).where(Extraction.email_id == email.id)
                        .values(email_message_id=msg.id))
        created += 1
    rows = session.scalars(
        select(Extraction)
        .where(Extraction.payment_key.is_(None), Extraction.is_remittance.is_(True))
        .order_by(Extraction.reviewed_at.asc().nulls_last(), Extraction.created_at.asc(),
                  Extraction.id.asc())
    )
    for row in rows:
        found = payment_key_for(row.canonical or {})
        if found is None:
            continue
        key, strength = found
        live = strength == "strong" and row.status in KEY_LIVE
        clash = live and session.scalar(
            select(Extraction.id).where(
                Extraction.payment_key == key, Extraction.payment_key_strength == "strong",
                Extraction.status.in_(KEY_LIVE), Extraction.id != row.id,
            )
        )
        if clash:
            row.validation_flags = list(row.validation_flags or []) + [
                f"header: possible duplicate of payment {clash} (same reference)"
            ]
            conflicts += 1
        else:
            row.payment_key, row.payment_key_strength = key, strength
            keyed += 1
        session.flush()
    return created, keyed, conflicts
```
Approved rows sort first (by `reviewed_at`), so the approved payment keeps the key and the pending copy is the one flagged — as the test expects.

`cli.py`: add `_cmd_threads_backfill` mirroring `_cmd_ledger_backfill` (import `get_session` inside the function, call `backfill_module.backfill(session)` via `from ar_pipeline.threads import backfill as backfill_module` so the monkeypatch applies), register the subparser `threads-backfill` ("assign payment keys and thread messages to rows created before threads"), dispatch it, and add it to the module docstring.

`.env.example`: add, with comments,
```
# Threads: your own mail domains / company names (internal forwards, payer vs beneficiary)
CLIENT_DOMAINS=
CLIENT_NAMES=
# Payments dated before this are treated as history (never auto-sent). YYYY-MM-DD
GO_LIVE_DATE=
```
README: a short "Email threads" section — what is split, "seen before", history and "Already recorded", and the rollout order: `scripts/dev_db.py migrate` → `ar-pipeline threads-backfill` → `ar-pipeline ledger-backfill`.

- [ ] **Step 4: Full verification**
- `uv run ruff check ar_pipeline tests && uv run ruff format --check ar_pipeline tests`
- `uv run mypy ar_pipeline`
- `uv run pytest -q` (whole suite, ~25 s) → all pass.
- Offline rehearsal (no dev data touched): run the four scenarios in `tests/threads/test_normalize_threads.py` once more with `LLM_PROVIDER=stub` semantics by adding one test that uses `StubLLMClient()` on `chain_html([2, 1])` then on `chain_html([3, 2, 1])`, asserting 2 then 1 AI-read messages (wrap the stub in a counting adapter). Stub-specific extraction details (payer, invoice) are not asserted.

- [ ] **Step 5: Commit**
```bash
git add ar_pipeline/threads/backfill.py ar_pipeline/cli.py .env.example README.md tests/threads/test_backfill.py tests/test_cli.py
git commit -m "feat(threads): backfill command, settings docs, end-to-end checks"
```
