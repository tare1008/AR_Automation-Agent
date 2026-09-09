# Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the project skeleton — canonical remittance schema, database models and migrations, config, blob storage, a stub backend, and a worker loop with no-op jobs — so later plans have a tested foundation to build on.

**Architecture:** One Python package `ar_pipeline` (FastAPI app + in-process APScheduler worker) plus a separate `stub_backend` FastAPI app. Sync SQLAlchemy 2.0 over Postgres, Alembic migrations. The canonical schema is a Pydantic model that generates JSON Schema; it is the single source of truth shared by normalization, the review UI, and the stub backend.

**Tech Stack:** Python 3.12, uv (packaging), FastAPI, uvicorn, SQLAlchemy 2.0 (sync) + psycopg 3, Alembic, Pydantic 2 + pydantic-settings, APScheduler 3, pytest + httpx, `pgserver` (embedded PostgreSQL 16 for local dev + tests — no Docker on this machine).

**Environment note:** This machine has `uv` but **no Docker and no system PostgreSQL**, and no passwordless sudo. Local dev and the test suite use the `pgserver` PyPI package, which bundles PostgreSQL 16 binaries and runs a private instance over a Unix socket under `.pgdata/`. Production still targets a managed Postgres (per the spec) — `pgserver` is dev/test only.

**Spec:** `docs/superpowers/specs/2026-09-09-ar-email-extraction-design.md`

## Global Constraints

- Python 3.12; manage deps with `uv` (`uv add`, `uv run`).
- Local dev + tests use `pgserver` (embedded PostgreSQL) — never assume Docker or a `localhost:5432` server. The test suite starts its own instance; no external DB setup.
- Sync SQLAlchemy only — no async engine/session in this project.
- No import-time database connections: `ar_pipeline/db/base.py` exposes lazy accessors (`get_engine()`, `get_session()`), never a module-level `engine` built at import.
- Currency default is `INR`; the `currency` field is a 3-letter ISO-4217 string.
- All monetary amounts are `Decimal` in Python / `NUMERIC` in Postgres — never `float`.
- Primary keys are UUID v4.
- Status fields are stored as `VARCHAR` with a `CHECK` constraint (portable enums), not native Postgres enums.
- Every task ends with a passing `uv run pytest` and a commit.
- No network calls in unit tests; the stub backend and any HTTP client are tested with `httpx` against in-process ASGI apps.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.python-version`
- Create: `scripts/dev_db.py`
- Create: `README.md`
- Create: `ar_pipeline/__init__.py`
- Create: `stub_backend/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/test_smoke.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a working `uv` project where `uv run pytest` passes; `uv run python scripts/dev_db.py` prints a ready-to-use `DATABASE_URL` for an embedded `pgserver` instance under `.pgdata/`.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "ar-pipeline"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "sqlalchemy>=2.0.36",
    "psycopg[binary]>=3.2",
    "alembic>=1.14",
    "pydantic>=2.10",
    "pydantic-settings>=2.6",
    "apscheduler>=3.10,<4",
    "httpx>=0.28",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-cov>=6.0",
    "pytest-asyncio>=0.24",
    "pgserver>=0.1.4",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra"
asyncio_mode = "auto"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["ar_pipeline", "stub_backend"]
```

- [ ] **Step 2: Create supporting files**

`.python-version`:
```
3.12
```

`.gitignore` (note: the repo root `.gitignore` already lists most of these from an earlier commit — ensure the file ends up with at least this set, `.pgdata/` included):
```
__pycache__/
*.pyc
.venv/
.pytest_cache/
.coverage
htmlcov/
*.egg-info/
.env
data/blob/
.pgdata/
.superpowers/
.worktrees/
```

`scripts/dev_db.py` — starts (or reuses) an embedded Postgres and ensures the app + test databases exist:
```python
"""Start a local embedded PostgreSQL (pgserver) for development.

Prints a DATABASE_URL you can export. The data lives under .pgdata/ and
persists between runs; delete that directory to reset.
"""

from __future__ import annotations

import pathlib

import pgserver

PGDATA = pathlib.Path(__file__).resolve().parent.parent / ".pgdata"


def ensure_server() -> pgserver.PostgresServer:
    PGDATA.mkdir(exist_ok=True)
    server = pgserver.get_server(str(PGDATA))
    for name in ("ar_pipeline", "ar_pipeline_test"):
        exists = server.psql(
            f"SELECT 1 FROM pg_database WHERE datname = '{name}'"
        ).strip()
        if "1" not in exists:
            server.psql(f"CREATE DATABASE {name}")
    return server


def uri_for(server: pgserver.PostgresServer, database: str) -> str:
    return server.get_uri(database=database).replace(
        "postgresql://", "postgresql+psycopg://", 1
    )


if __name__ == "__main__":
    srv = ensure_server()
    print("DATABASE_URL=" + uri_for(srv, "ar_pipeline"))
    print("TEST_DATABASE_URL=" + uri_for(srv, "ar_pipeline_test"))
```

`README.md`:
```markdown
# AR Email Settlement Extraction Pipeline

## Setup
    uv sync

This machine has no Docker; local Postgres is an embedded `pgserver`
instance (bundled binaries, Unix socket under `.pgdata/`). The test
suite starts its own instance automatically. For a dev DB:

    eval "$(uv run python scripts/dev_db.py | sed 's/^/export /')"
    uv run alembic upgrade head

## Test
    uv run pytest

## Run
    uv run uvicorn ar_pipeline.main:app --reload
    uv run uvicorn stub_backend.app:app --port 9000 --reload
```

Create empty `ar_pipeline/__init__.py`, `stub_backend/__init__.py`, `tests/__init__.py`.

- [ ] **Step 3: Write the smoke test**

`tests/test_smoke.py`:
```python
import ar_pipeline
import stub_backend


def test_packages_import():
    assert ar_pipeline is not None
    assert stub_backend is not None
```

- [ ] **Step 4: Install and run**

Run: `uv sync && uv run pytest tests/test_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: project scaffold"
```

(If git is not yet initialised, skip the commit for this task and run all commits once `git init` is done — see the project note. Continue implementing regardless.)

---

### Task 2: Canonical remittance schema

**Files:**
- Create: `ar_pipeline/schema/__init__.py`
- Create: `ar_pipeline/schema/canonical.py`
- Create: `tests/schema/__init__.py`
- Create: `tests/schema/test_canonical.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `RemittancePayload` (Pydantic `BaseModel`) with attributes `envelope: Envelope`, `header: Header`, `line_items: list[LineItem]`.
  - `Envelope(extraction_id: str, source_email_id: str, vendor_guess: str | None, extracted_at: datetime, reviewed_by: str | None)`.
  - `Header(payer_name: str, payer_id: str | None, payment_reference: str, payment_date: date, payment_method: str | None, currency: str = "INR", total_paid_amount: Decimal)`.
  - `LineItem(invoice_number: str, invoice_date: date | None, invoice_amount: Decimal, discount_taken: Decimal | None, deduction_amount: Decimal | None, deduction_reason: str | None, amount_paid: Decimal)`.
  - `CANONICAL_JSON_SCHEMA: dict` — `RemittancePayload.model_json_schema()`.

- [ ] **Step 1: Write the failing test**

`tests/schema/test_canonical.py`:
```python
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ar_pipeline.schema.canonical import (
    CANONICAL_JSON_SCHEMA,
    Header,
    LineItem,
    RemittancePayload,
)


def _valid_payload_dict():
    return {
        "envelope": {
            "extraction_id": "ext-1",
            "source_email_id": "email-1",
            "vendor_guess": "Acme Corp",
            "extracted_at": datetime(2026, 9, 9, tzinfo=timezone.utc).isoformat(),
            "reviewed_by": None,
        },
        "header": {
            "payer_name": "Acme Corp",
            "payer_id": None,
            "payment_reference": "EFT-88213",
            "payment_date": "2026-09-05",
            "payment_method": "ACH",
            "currency": "INR",
            "total_paid_amount": "12450.00",
        },
        "line_items": [
            {
                "invoice_number": "INV-1001",
                "invoice_date": "2026-08-01",
                "invoice_amount": "5000.00",
                "discount_taken": "100.00",
                "deduction_amount": "0.00",
                "deduction_reason": None,
                "amount_paid": "4900.00",
            }
        ],
    }


def test_valid_payload_parses():
    payload = RemittancePayload.model_validate(_valid_payload_dict())
    assert payload.header.total_paid_amount == Decimal("12450.00")
    assert payload.header.currency == "INR"
    assert payload.line_items[0].invoice_date == date(2026, 8, 1)


def test_currency_defaults_to_inr():
    h = Header(
        payer_name="X",
        payer_id=None,
        payment_reference="R",
        payment_date=date(2026, 9, 5),
        payment_method=None,
        total_paid_amount=Decimal("1.00"),
    )
    assert h.currency == "INR"


def test_amounts_reject_float_noise():
    li = LineItem.model_validate(
        {
            "invoice_number": "INV-1",
            "invoice_date": None,
            "invoice_amount": "10.10",
            "discount_taken": None,
            "deduction_amount": None,
            "deduction_reason": None,
            "amount_paid": "10.10",
        }
    )
    assert li.invoice_amount == Decimal("10.10")


def test_missing_required_field_rejected():
    bad = _valid_payload_dict()
    del bad["header"]["payment_reference"]
    with pytest.raises(ValidationError):
        RemittancePayload.model_validate(bad)


def test_currency_must_be_three_letters():
    bad = _valid_payload_dict()
    bad["header"]["currency"] = "Rupees"
    with pytest.raises(ValidationError):
        RemittancePayload.model_validate(bad)


def test_currency_rejects_non_alphabetic():
    bad = _valid_payload_dict()
    bad["header"]["currency"] = "1N5"
    with pytest.raises(ValidationError):
        RemittancePayload.model_validate(bad)


def test_currency_is_uppercased():
    d = _valid_payload_dict()
    d["header"]["currency"] = "inr"
    assert RemittancePayload.model_validate(d).header.currency == "INR"


def test_json_schema_is_dict_with_defs():
    assert isinstance(CANONICAL_JSON_SCHEMA, dict)
    assert CANONICAL_JSON_SCHEMA["title"] == "RemittancePayload"
    assert "$defs" in CANONICAL_JSON_SCHEMA
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/schema/test_canonical.py -v`
Expected: FAIL — `ModuleNotFoundError: ar_pipeline.schema.canonical`

- [ ] **Step 3: Implement the schema**

`ar_pipeline/schema/canonical.py`:
```python
"""Canonical remittance payload — the single source of truth for the
shape of extracted settlement data. Referenced by normalization,
the review UI, and the stub backend."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

_Currency = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z]{3}$", to_upper=True)
]


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extraction_id: str
    source_email_id: str
    vendor_guess: str | None = None
    extracted_at: datetime
    reviewed_by: str | None = None


class Header(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payer_name: str
    payer_id: str | None = None
    payment_reference: str
    payment_date: date
    payment_method: str | None = None
    currency: _Currency = "INR"
    total_paid_amount: Decimal


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_number: str
    invoice_date: date | None = None
    invoice_amount: Decimal
    discount_taken: Decimal | None = None
    deduction_amount: Decimal | None = None
    deduction_reason: str | None = None
    amount_paid: Decimal


class RemittancePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    envelope: Envelope
    header: Header
    line_items: list[LineItem] = Field(min_length=1)


CANONICAL_JSON_SCHEMA: dict = RemittancePayload.model_json_schema()
```

`ar_pipeline/schema/__init__.py`:
```python
from ar_pipeline.schema.canonical import (
    CANONICAL_JSON_SCHEMA,
    Envelope,
    Header,
    LineItem,
    RemittancePayload,
)

__all__ = [
    "CANONICAL_JSON_SCHEMA",
    "Envelope",
    "Header",
    "LineItem",
    "RemittancePayload",
]
```

Create empty `tests/schema/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/schema/test_canonical.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add ar_pipeline/schema tests/schema
git commit -m "feat: canonical remittance schema"
```

---

### Task 3: Config

**Files:**
- Create: `ar_pipeline/config.py`
- Create: `tests/test_config.py`
- Create: `.env.example`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Settings` (pydantic-settings `BaseSettings`) with fields:
    `database_url: str`, `test_database_url: str`, `blob_dir: str = "data/blob"`,
    `graph_tenant_id: str = ""`, `graph_client_id: str = ""`, `graph_client_secret: str = ""`,
    `shared_mailbox: str = ""`, `backend_url: str = ""`, `backend_auth_header: str = ""`,
    `llm_provider: str = "anthropic"`, `poll_interval_seconds: int = 300`,
    `advance_interval_seconds: int = 60`, `deliver_interval_seconds: int = 60`.
  - `get_settings() -> Settings` — cached accessor (`functools.lru_cache`).

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:
```python
import ar_pipeline.config as config_module
from ar_pipeline.config import Settings, get_settings


def test_settings_reads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://u:p@localhost/db_test")
    s = Settings()
    assert s.database_url.endswith("/db")
    assert s.poll_interval_seconds == 300
    assert s.llm_provider == "anthropic"


def test_get_settings_is_cached(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://u:p@localhost/db_test")
    config_module.get_settings.cache_clear()
    assert get_settings() is get_settings()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: ar_pipeline.config`

- [ ] **Step 3: Implement config**

`ar_pipeline/config.py`:
```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ar:ar@localhost:5432/ar_pipeline"
    test_database_url: str = "postgresql+psycopg://ar:ar@localhost:5432/ar_pipeline_test"
    blob_dir: str = "data/blob"

    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    shared_mailbox: str = ""

    backend_url: str = ""
    backend_auth_header: str = ""

    llm_provider: str = "anthropic"

    poll_interval_seconds: int = 300
    advance_interval_seconds: int = 60
    deliver_interval_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

`.env.example`:
```
DATABASE_URL=postgresql+psycopg://ar:ar@localhost:5432/ar_pipeline
TEST_DATABASE_URL=postgresql+psycopg://ar:ar@localhost:5432/ar_pipeline_test
BLOB_DIR=data/blob
GRAPH_TENANT_ID=
GRAPH_CLIENT_ID=
GRAPH_CLIENT_SECRET=
SHARED_MAILBOX=
BACKEND_URL=
BACKEND_AUTH_HEADER=
LLM_PROVIDER=anthropic
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ar_pipeline/config.py tests/test_config.py .env.example
git commit -m "feat: settings/config"
```

---

### Task 4: Database models

**Files:**
- Create: `ar_pipeline/db/__init__.py`
- Create: `ar_pipeline/db/base.py`
- Create: `ar_pipeline/db/models.py`
- Create: `tests/conftest.py`
- Create: `tests/db/__init__.py`
- Create: `tests/db/test_models.py`

**Interfaces:**
- Consumes: `ar_pipeline.config.get_settings`.
- Produces:
  - `ar_pipeline.db.base.Base` — declarative base.
  - `ar_pipeline.db.base.get_engine()` — lazily-created (`lru_cache`) sync engine bound to `get_settings().database_url`. No engine is built at import time.
  - `ar_pipeline.db.base.get_sessionmaker()` — lazily-created `sessionmaker`.
  - `ar_pipeline.db.base.get_session()` — context-manager yielding a `Session` (commits on success, rolls back on exception).
  - `ar_pipeline.db.base.reset_engine()` — disposes and clears the cached engine/sessionmaker (used by tests after changing env).
  - ORM models in `ar_pipeline.db.models`: `PollState`, `Email`, `Attachment`, `ExtractionSource`, `RawExtraction`, `Extraction`, `ExtractionEdit`, `Delivery`, `Vendor`.
  - Status string constants: `EMAIL_STATUSES = ("new","classified","extracted","normalized","review","done","error")`, `EXTRACTION_STATUSES = ("pending_review","approved","rejected")`, `DELIVERY_STATUSES = ("pending","delivered","failed")`.
  - pytest fixture `db_session` (function-scoped, rolls back after each test).

- [ ] **Step 1: Write the failing test**

`tests/db/test_models.py`:
```python
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from ar_pipeline.db.models import Attachment, Email


def test_insert_and_query_email(db_session):
    email = Email(
        internet_message_id="<msg-1@vendor.com>",
        sender_address="ap@vendor.com",
        sender_domain="vendor.com",
        subject="Remittance",
        received_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        body_html="<p>hi</p>",
        body_text="hi",
        raw_headers={},
        status="new",
    )
    db_session.add(email)
    db_session.flush()
    assert email.id is not None

    got = db_session.get(Email, email.id)
    assert got.sender_domain == "vendor.com"
    assert got.status == "new"


def test_internet_message_id_is_unique(db_session):
    for _ in range(2):
        db_session.add(
            Email(
                internet_message_id="<dupe@vendor.com>",
                sender_address="ap@vendor.com",
                sender_domain="vendor.com",
                subject="x",
                received_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
                body_html="",
                body_text="",
                raw_headers={},
                status="new",
            )
        )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_bad_status_rejected(db_session):
    db_session.add(
        Email(
            internet_message_id="<bad-status@vendor.com>",
            sender_address="ap@vendor.com",
            sender_domain="vendor.com",
            subject="x",
            received_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
            body_html="",
            body_text="",
            raw_headers={},
            status="banana",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_attachment_belongs_to_email(db_session):
    email = Email(
        internet_message_id="<att@vendor.com>",
        sender_address="ap@vendor.com",
        sender_domain="vendor.com",
        subject="x",
        received_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        body_html="",
        body_text="",
        raw_headers={},
        status="new",
    )
    db_session.add(email)
    db_session.flush()
    att = Attachment(
        email_id=email.id,
        filename="settlement.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size=1234,
        blob_url="file://data/blob/abc",
        sha256="0" * 64,
    )
    db_session.add(att)
    db_session.flush()
    assert att.id is not None
```

- [ ] **Step 2: Write the declarative base and session**

`ar_pipeline/db/base.py`:
```python
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ar_pipeline.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, future=True)


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, class_=Session)


def reset_engine() -> None:
    """Dispose and forget the cached engine — for tests that change env."""
    try:
        get_engine().dispose()
    finally:
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()


@contextmanager
def get_session():
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

`ar_pipeline/db/__init__.py`:
```python
from ar_pipeline.db.base import (
    Base,
    get_engine,
    get_session,
    get_sessionmaker,
    reset_engine,
)

__all__ = [
    "Base",
    "get_engine",
    "get_session",
    "get_sessionmaker",
    "reset_engine",
]
```

- [ ] **Step 3: Write the models**

`ar_pipeline/db/models.py`:
```python
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ar_pipeline.db.base import Base

EMAIL_STATUSES = (
    "new",
    "classified",
    "extracted",
    "normalized",
    "review",
    "done",
    "error",
)
EXTRACTION_STATUSES = ("pending_review", "approved", "rejected")
DELIVERY_STATUSES = ("pending", "delivered", "failed")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _in(col: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{col} IN ({joined})"


class PollState(Base):
    __tablename__ = "poll_state"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    delta_token: Mapped[str | None] = mapped_column(Text)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("id = 1", name="poll_state_singleton"),)


class Email(Base):
    __tablename__ = "email"

    id: Mapped[uuid.UUID] = _uuid_pk()
    internet_message_id: Mapped[str] = mapped_column(String(998))
    sender_address: Mapped[str] = mapped_column(String(320))
    sender_domain: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    body_html: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    raw_headers: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="new")
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    attachments: Mapped[list[Attachment]] = relationship(back_populates="email")

    __table_args__ = (
        UniqueConstraint("internet_message_id", name="uq_email_internet_message_id"),
        CheckConstraint(_in("status", EMAIL_STATUSES), name="ck_email_status"),
    )


class Attachment(Base):
    __tablename__ = "attachment"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("email.id"))
    filename: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(255), default="")
    size: Mapped[int] = mapped_column(default=0)
    blob_url: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))

    email: Mapped[Email] = relationship(back_populates="attachments")


class ExtractionSource(Base):
    __tablename__ = "extraction_source"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("email.id"))
    kind: Mapped[str] = mapped_column(String(20))
    ref: Mapped[str] = mapped_column(Text)  # 'body' or attachment id
    skipped: Mapped[bool] = mapped_column(default=False)
    skip_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            _in("kind", ("body_table", "excel", "pdf_text", "pdf_scanned", "image")),
            name="ck_extraction_source_kind",
        ),
    )


class RawExtraction(Base):
    __tablename__ = "raw_extraction"

    id: Mapped[uuid.UUID] = _uuid_pk()
    extraction_source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extraction_source.id"))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    extractor_version: Mapped[str] = mapped_column(String(50), default="")


class Extraction(Base):
    __tablename__ = "extraction"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("email.id"))
    canonical: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    is_remittance: Mapped[bool] = mapped_column(default=True)
    validation_flags: Mapped[list] = mapped_column(JSONB, default=list)
    llm_model: Mapped[str] = mapped_column(String(100), default="")
    prompt_version: Mapped[str] = mapped_column(String(50), default="")
    raw_llm_response: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending_review")
    reviewed_by: Mapped[str | None] = mapped_column(String(320))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(_in("status", EXTRACTION_STATUSES), name="ck_extraction_status"),
    )


class ExtractionEdit(Base):
    __tablename__ = "extraction_edit"

    id: Mapped[uuid.UUID] = _uuid_pk()
    extraction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extraction.id"))
    field_path: Mapped[str] = mapped_column(Text)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    edited_by: Mapped[str] = mapped_column(String(320))
    edited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Delivery(Base):
    __tablename__ = "delivery"

    id: Mapped[uuid.UUID] = _uuid_pk()
    extraction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("extraction.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(_in("status", DELIVERY_STATUSES), name="ck_delivery_status"),
    )


class Vendor(Base):
    __tablename__ = "vendor"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    sender_domains: Mapped[list[str]] = mapped_column(ARRAY(String(255)), default=list)
    format_hint: Mapped[str | None] = mapped_column(Text)
    column_hints: Mapped[dict] = mapped_column(JSONB, default=dict)
    active: Mapped[bool] = mapped_column(default=True)
```

- [ ] **Step 4: Write the test fixtures**

`tests/conftest.py` — a session-scoped, autouse fixture starts one embedded
Postgres for the whole run and points the app's settings at it **before**
any code reads `get_settings()`:
```python
import os
import pathlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

PGDATA = pathlib.Path(__file__).resolve().parent.parent / ".pgdata"


@pytest.fixture(scope="session", autouse=True)
def _embedded_pg():
    import pgserver

    PGDATA.mkdir(exist_ok=True)
    server = pgserver.get_server(str(PGDATA))
    for name in ("ar_pipeline", "ar_pipeline_test"):
        exists = server.psql(
            f"SELECT 1 FROM pg_database WHERE datname = '{name}'"
        ).strip()
        if "1" not in exists:
            server.psql(f"CREATE DATABASE {name}")

    def uri(database: str) -> str:
        return server.get_uri(database=database).replace(
            "postgresql://", "postgresql+psycopg://", 1
        )

    os.environ["DATABASE_URL"] = uri("ar_pipeline")
    os.environ["TEST_DATABASE_URL"] = uri("ar_pipeline_test")

    from ar_pipeline.config import get_settings
    from ar_pipeline.db.base import reset_engine

    get_settings.cache_clear()
    reset_engine()

    yield server
    # leave the server running for reuse across local runs; pgserver
    # reference-counts and cleans up when no processes remain.


@pytest.fixture(scope="session")
def _test_engine(_embedded_pg):
    from ar_pipeline.config import get_settings
    from ar_pipeline.db.base import Base
    from ar_pipeline.db import models  # noqa: F401  (register mappers)

    engine = create_engine(get_settings().test_database_url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(_test_engine):
    connection = _test_engine.connect()
    trans = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()
```

Create empty `tests/db/__init__.py`.

> Note for the implementer: confirm the `pgserver` API against the installed
> version (`uv run python -c "import pgserver, inspect; print([n for n in dir(pgserver)])"`).
> `get_server(datadir)` and `PostgresServer.get_uri(database=...)` / `.psql(sql)`
> are expected; if `get_uri` has no `database` kwarg, build the URI by
> swapping the trailing `/postgres` path segment while keeping the
> `?host=<socket dir>` query string.

- [ ] **Step 5: Run tests**

The `_embedded_pg` fixture creates the databases automatically — no external setup.

Run: `uv run pytest tests/db/test_models.py -v`
Expected: PASS (4 tests)

If `pgserver` fails to start (first run downloads/extracts bundled binaries — allow a minute), run `uv run python scripts/dev_db.py` once and re-run.

- [ ] **Step 6: Commit**

```bash
git add ar_pipeline/db tests/conftest.py tests/db pyproject.toml uv.lock
git commit -m "feat: database models and test fixtures"
```

---

### Task 5: Alembic migrations

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/` (dir, keep with `.gitkeep`)
- Create: `migrations/versions/0001_initial.py` (autogenerated, then reviewed)
- Create: `tests/db/test_migrations.py`

**Interfaces:**
- Consumes: `ar_pipeline.db.base.Base`, `ar_pipeline.config.get_settings`.
- Produces: `uv run alembic upgrade head` builds the full schema; `alembic downgrade base` tears it down.

- [ ] **Step 1: Initialise Alembic**

Run: `uv run alembic init -t generic migrations`
Then edit `alembic.ini` — set `script_location = migrations` and remove the hardcoded `sqlalchemy.url` line (env.py will supply it).

- [ ] **Step 2: Wire `migrations/env.py` to the app**

Replace the generated `migrations/env.py` body so it reads:
```python
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from ar_pipeline.config import get_settings
from ar_pipeline.db.base import Base
from ar_pipeline.db import models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 3: Autogenerate the initial migration**

Run:
```bash
eval "$(uv run python scripts/dev_db.py | sed 's/^/export /')"
uv run alembic revision --autogenerate -m "initial" --rev-id 0001
```
(`dev_db.py` prints `DATABASE_URL=...` / `TEST_DATABASE_URL=...`; `eval` exports both so `migrations/env.py` — which reads `get_settings().database_url` — points at the embedded Postgres.)

Open `migrations/versions/0001_initial.py` and confirm it creates all nine tables (`poll_state`, `email`, `attachment`, `extraction_source`, `raw_extraction`, `extraction`, `extraction_edit`, `delivery`, `vendor`) with the unique + check constraints. Fix by hand if autogenerate missed any check constraint (Alembic often omits `CheckConstraint`s that were passed inline — add explicit `op.create_check_constraint(...)` calls and their `op.drop_constraint(...)` counterparts).

- [ ] **Step 4: Write the migration round-trip test**

`tests/db/test_migrations.py` — depends on `_embedded_pg` so `DATABASE_URL`
is exported into this process (and inherited by the alembic subprocesses):
```python
import subprocess


def _alembic(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        capture_output=True, text=True,
    )


def test_migrations_upgrade_and_downgrade(_embedded_pg):
    up = _alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    down = _alembic("downgrade", "base")
    assert down.returncode == 0, down.stderr

    again = _alembic("upgrade", "head")
    assert again.returncode == 0, again.stderr
```

- [ ] **Step 5: Run the test**

Run: `uv run pytest tests/db/test_migrations.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add alembic.ini migrations
git commit -m "feat: alembic migrations (initial schema)"
```

---

### Task 6: Blob storage

**Files:**
- Create: `ar_pipeline/storage.py`
- Create: `tests/test_storage.py`

**Interfaces:**
- Consumes: `ar_pipeline.config.get_settings`.
- Produces:
  - `BlobStore` (Protocol): `put(key: str, data: bytes) -> str` (returns a URL), `get(key: str) -> bytes`, `sha256(data: bytes) -> str`.
  - `LocalBlobStore(root: str)` — writes under `root`, returns `file://<abs path>`.
  - `get_blob_store() -> BlobStore` — returns a `LocalBlobStore(get_settings().blob_dir)`.

- [ ] **Step 1: Write the failing test**

`tests/test_storage.py`:
```python
import hashlib

from ar_pipeline.storage import LocalBlobStore


def test_put_then_get_roundtrips(tmp_path):
    store = LocalBlobStore(str(tmp_path))
    url = store.put("vendor/msg1/settlement.xlsx", b"binary-bytes")
    assert url.startswith("file://")
    assert store.get("vendor/msg1/settlement.xlsx") == b"binary-bytes"


def test_sha256_matches_hashlib():
    store = LocalBlobStore("/tmp")
    assert store.sha256(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_get_missing_key_raises(tmp_path):
    store = LocalBlobStore(str(tmp_path))
    try:
        store.get("nope")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storage.py -v`
Expected: FAIL — `ModuleNotFoundError: ar_pipeline.storage`

- [ ] **Step 3: Implement storage**

`ar_pipeline/storage.py`:
```python
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from ar_pipeline.config import get_settings


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...
    def sha256(self, data: bytes) -> str: ...


class LocalBlobStore:
    def __init__(self, root: str) -> None:
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        return self._root / key

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"file://{path.resolve()}"

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def sha256(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


def get_blob_store() -> BlobStore:
    return LocalBlobStore(get_settings().blob_dir)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_storage.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ar_pipeline/storage.py tests/test_storage.py
git commit -m "feat: blob storage (local impl)"
```

---

### Task 7: Stub backend

**Files:**
- Create: `stub_backend/app.py`
- Create: `stub_backend/store.py`
- Create: `tests/stub_backend/__init__.py`
- Create: `tests/stub_backend/test_app.py`

**Interfaces:**
- Consumes: `ar_pipeline.schema.canonical.RemittancePayload`.
- Produces:
  - `stub_backend.app.app` — FastAPI app.
  - `POST /remittances` — body validated as `RemittancePayload`; returns `201` with `{"id": <extraction_id>, "status": "received"}`; honours an idempotency header `Idempotency-Key` (repeat key → `200` with the same body, not a second store).
  - `GET /remittances/{extraction_id}` — returns the stored payload or `404`.
  - `stub_backend.store.RECEIVED: dict[str, dict]` — in-memory store (reset per process).

- [ ] **Step 1: Write the failing test**

`tests/stub_backend/test_app.py`:
```python
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from stub_backend.app import app
from stub_backend.store import RECEIVED


def _payload():
    return {
        "envelope": {
            "extraction_id": "ext-42",
            "source_email_id": "email-1",
            "vendor_guess": None,
            "extracted_at": datetime(2026, 9, 9, tzinfo=timezone.utc).isoformat(),
            "reviewed_by": "u@co.com",
        },
        "header": {
            "payer_name": "Acme",
            "payer_id": None,
            "payment_reference": "EFT-1",
            "payment_date": "2026-09-05",
            "payment_method": None,
            "currency": "INR",
            "total_paid_amount": "100.00",
        },
        "line_items": [
            {
                "invoice_number": "INV-1",
                "invoice_date": None,
                "invoice_amount": "100.00",
                "discount_taken": None,
                "deduction_amount": None,
                "deduction_reason": None,
                "amount_paid": "100.00",
            }
        ],
    }


@pytest.fixture(autouse=True)
def _clear_store():
    RECEIVED.clear()
    yield
    RECEIVED.clear()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_valid_payload_accepted(client):
    r = await client.post("/remittances", json=_payload())
    assert r.status_code == 201
    assert r.json()["id"] == "ext-42"
    assert "ext-42" in RECEIVED


async def test_invalid_payload_rejected(client):
    bad = _payload()
    del bad["header"]["payment_reference"]
    r = await client.post("/remittances", json=bad)
    assert r.status_code == 422


async def test_idempotency_key_dedupes(client):
    headers = {"Idempotency-Key": "abc-123"}
    r1 = await client.post("/remittances", json=_payload(), headers=headers)
    r2 = await client.post("/remittances", json=_payload(), headers=headers)
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert len(RECEIVED) == 1


async def test_get_returns_stored_payload(client):
    await client.post("/remittances", json=_payload())
    r = await client.get("/remittances/ext-42")
    assert r.status_code == 200
    assert r.json()["header"]["currency"] == "INR"


async def test_get_missing_is_404(client):
    r = await client.get("/remittances/nope")
    assert r.status_code == 404
```

(`pytest-asyncio` and `asyncio_mode = "auto"` were already added in Task 1 — nothing to change in `pyproject.toml` here. Verify with `grep asyncio_mode pyproject.toml`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/stub_backend/test_app.py -v`
Expected: FAIL — `ModuleNotFoundError: stub_backend.app`

- [ ] **Step 3: Implement the stub backend**

`stub_backend/store.py`:
```python
RECEIVED: dict[str, dict] = {}
_IDEMPOTENCY: dict[str, str] = {}  # idempotency key -> extraction_id
```

`stub_backend/app.py`:
```python
from fastapi import FastAPI, Header, Response

from ar_pipeline.schema.canonical import RemittancePayload
from stub_backend.store import _IDEMPOTENCY, RECEIVED

app = FastAPI(title="AR stub backend")


@app.post("/remittances")
def receive(payload: RemittancePayload, response: Response,
            idempotency_key: str | None = Header(default=None)):
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
```

Create empty `tests/stub_backend/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/stub_backend/test_app.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add stub_backend tests/stub_backend pyproject.toml
git commit -m "feat: stub backend validating the canonical schema"
```

---

### Task 8: Worker skeleton and app entrypoint

**Files:**
- Create: `ar_pipeline/worker.py`
- Create: `ar_pipeline/main.py`
- Create: `tests/test_worker.py`

**Interfaces:**
- Consumes: `ar_pipeline.config.get_settings`.
- Produces:
  - `ar_pipeline.worker.build_scheduler() -> BackgroundScheduler` — registers three jobs by id: `poll_inbox`, `advance_pipeline`, `run_deliveries`, each on its configured interval.
  - `ar_pipeline.worker.poll_inbox()`, `advance_pipeline()`, `run_deliveries()` — no-op functions that log `"<name>: no-op"` at INFO and return `None`. Later plans replace the bodies.
  - `ar_pipeline.main.app` — FastAPI app that starts the scheduler on startup and shuts it down on shutdown; `GET /healthz` returns `{"status": "ok"}`.

- [ ] **Step 1: Write the failing test**

`tests/test_worker.py`:
```python
from httpx import ASGITransport, AsyncClient

from ar_pipeline import worker
from ar_pipeline.main import app


def test_scheduler_registers_three_jobs():
    sched = worker.build_scheduler()
    ids = {j.id for j in sched.get_jobs()}
    assert ids == {"poll_inbox", "advance_pipeline", "run_deliveries"}


def test_noop_jobs_return_none(caplog):
    import logging

    caplog.set_level(logging.INFO)
    assert worker.poll_inbox() is None
    assert worker.advance_pipeline() is None
    assert worker.run_deliveries() is None
    assert "poll_inbox: no-op" in caplog.text


async def test_healthz():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker.py -v`
Expected: FAIL — `ModuleNotFoundError: ar_pipeline.worker`

- [ ] **Step 3: Implement worker and main**

`ar_pipeline/worker.py`:
```python
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from ar_pipeline.config import get_settings

log = logging.getLogger(__name__)


def poll_inbox() -> None:
    log.info("poll_inbox: no-op")
    return None


def advance_pipeline() -> None:
    log.info("advance_pipeline: no-op")
    return None


def run_deliveries() -> None:
    log.info("run_deliveries: no-op")
    return None


def build_scheduler() -> BackgroundScheduler:
    s = get_settings()
    scheduler = BackgroundScheduler()
    scheduler.add_job(poll_inbox, "interval", seconds=s.poll_interval_seconds, id="poll_inbox")
    scheduler.add_job(
        advance_pipeline, "interval", seconds=s.advance_interval_seconds, id="advance_pipeline"
    )
    scheduler.add_job(
        run_deliveries, "interval", seconds=s.deliver_interval_seconds, id="run_deliveries"
    )
    return scheduler
```

`ar_pipeline/main.py`:
```python
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ar_pipeline.worker import build_scheduler

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = build_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="AR pipeline", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_worker.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest`
Expected: PASS (all tasks)

- [ ] **Step 6: Commit**

```bash
git add ar_pipeline/worker.py ar_pipeline/main.py tests/test_worker.py
git commit -m "feat: worker skeleton and app entrypoint"
```

---

## Self-Review

**1. Spec coverage (Foundation portion):**
- Canonical schema (spec §"Canonical schema") → Task 2. ✓
- `schema/` single source of truth used by stub backend → Tasks 2, 7. ✓
- Data model / all nine tables (spec §"Data model") → Task 4; migrations Task 5. ✓
- `status` values and CHECK constraints → Task 4. ✓
- Blob storage abstraction with local impl + slot for Azure (spec §Modules) → Task 6. ✓
- Stub backend validating the same schema, idempotency key (spec §"stub_backend/", §deliver) → Task 7. ✓
- `worker.py` three jobs on timers (spec §"Processing model") → Task 8 (no-op bodies; later plans fill them). ✓
- Config for Graph/backend/LLM/intervals (spec §Decisions, §"Open items") → Task 3. ✓
- Deferred to later plans by design: ingest/classify/extract/normalize/review/deliver logic, real auth, LLM client.

**2. Placeholder scan:** No "TBD"/"handle errors"/"similar to Task N". The no-op worker bodies in Task 8 are an explicit, tested deliverable (logging stubs), not a placeholder — later plans replace them.

**3. Type consistency:** `RemittancePayload` / `Envelope` / `Header` / `LineItem` names match between Task 2, Task 7. Status tuples `EMAIL_STATUSES` / `EXTRACTION_STATUSES` / `DELIVERY_STATUSES` defined once in Task 4 and referenced by constraint names consistently. `BlobStore.put/get/sha256` signatures match between Task 6 definition and its test. Worker job ids (`poll_inbox`, `advance_pipeline`, `run_deliveries`) consistent between Task 8 impl and test.

## Execution Handoff

Handled in chat after the plan is saved.
