# Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Poll the shared Microsoft 365 mailbox via the Graph API, store new emails and their attachments, and wire this into the worker's `poll_inbox()` job — so the pipeline has real `email` / `attachment` rows to process.

**Architecture:** A `GraphClient` protocol with a real httpx+MSAL implementation and an in-memory fake for tests. A pure `poll_once(graph, blob_store, session)` orchestrator does the work (idempotent, delta-token based) and is fully tested against the fake + a real embedded Postgres. `worker.poll_inbox()` becomes a thin wrapper that builds the real client from settings and calls `poll_once`, skipping cleanly when Graph credentials are not configured.

**Tech Stack:** Python 3.12, httpx, msal, SQLAlchemy 2.0 (sync), pytest. Plus dev tooling introduced here: ruff, mypy, GitHub Actions CI.

**Spec:** `docs/superpowers/specs/2026-09-09-ar-email-extraction-design.md` (see the `ingest/` module section)

**Builds on:** the Foundation plan (merged to `master`, `fff6ada`). Available:
- `ar_pipeline.config.get_settings()` → `Settings` with `graph_tenant_id`, `graph_client_id`, `graph_client_secret: SecretStr`, `shared_mailbox`, `poll_interval_seconds`.
- `ar_pipeline.db.base`: `Base`, `get_engine()`, `get_sessionmaker()`, `get_session()` (contextmanager, commits on success), `reset_engine()`.
- `ar_pipeline.db.models`: `Email` (unique `internet_message_id`, `status` default `"new"`), `Attachment` (FK `email_id`, `filename`, `content_type`, `size`, `blob_url`, `sha256`), `PollState` (singleton `id=1`, `delta_token: str | None`, `last_poll_at: datetime | None`).
- `ar_pipeline.storage`: `BlobStore` protocol (`put(key, data)->str`, `get(key)->bytes`, `sha256(data)->str`), `LocalBlobStore`, `get_blob_store()`.
- `ar_pipeline.worker`: `poll_inbox()` / `advance_pipeline()` / `run_deliveries()` no-op jobs; `build_scheduler()`.
- Tests: `tests/conftest.py` provides session-scoped autouse `_embedded_pg` (embedded Postgres, sets `DATABASE_URL`/`TEST_DATABASE_URL`), `_test_engine`, and function-scoped `db_session` (savepoint rollback).

## Global Constraints

- Python 3.12; deps via `uv` (`uv add`, `uv add --dev`, `uv run`).
- Sync SQLAlchemy only.
- No import-time database connections or network calls anywhere in `ar_pipeline/`.
- No real network in tests — the real `GraphClient` is tested with `httpx.MockTransport`; the poller is tested with an in-memory `FakeGraphClient`.
- Every task ends with a passing `uv run pytest`, a clean `uv run ruff check`, a clean `uv run mypy ar_pipeline stub_backend`, and a commit.
- Commit message trailer: `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- Secrets: read `graph_client_secret` via `.get_secret_value()` only where MSAL needs it; never log it.
- New code lives under `ar_pipeline/ingest/`. One responsibility per file.

---

### Task 1: Dev tooling — ruff, mypy, CI

**Files:**
- Modify: `pyproject.toml`
- Create: `.github/workflows/ci.yml`
- Modify: whatever existing files ruff/mypy flag (expected: few, small)

**Interfaces:**
- Consumes: nothing.
- Produces: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy ar_pipeline stub_backend` all pass; a CI workflow that runs lint + types + tests.

- [ ] **Step 1: Add the tools**

```bash
uv add --dev ruff mypy
```

- [ ] **Step 2: Configure them in `pyproject.toml`**

Add:
```toml
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.mypy]
python_version = "3.12"
packages = ["ar_pipeline", "stub_backend"]
plugins = ["pydantic.mypy"]
warn_unused_ignores = true
warn_redundant_casts = true
no_implicit_optional = true
check_untyped_defs = true

[[tool.mypy.overrides]]
module = ["pgserver.*", "apscheduler.*"]
ignore_missing_imports = true
```

- [ ] **Step 3: Fix what they flag**

Run `uv run ruff check --fix` then `uv run ruff format`, then `uv run mypy ar_pipeline stub_backend`. Fix remaining issues by hand. Expected areas: import sorting, an unused import, a missing return annotation, `Optional` vs `| None`. Do **not** silence errors with blanket `# type: ignore` — fix the cause or add a scoped, commented ignore. If mypy flags something that needs a real design change, stop and report it as DONE_WITH_CONCERNS rather than papering over it.

- [ ] **Step 4: CI workflow**

`.github/workflows/ci.yml`:
```yaml
name: CI
on:
  push:
  pull_request:
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v5
      - run: uv sync --all-extras --dev
      - run: uv run ruff check
      - run: uv run ruff format --check
      - run: uv run mypy ar_pipeline stub_backend
      - run: uv run pytest -q
```

(No remote is configured yet, so this workflow will not run until the repo is pushed to GitHub — it is committed now so it is ready.)

- [ ] **Step 5: Verify and commit**

Run:
```
uv run ruff check
uv run ruff format --check
uv run mypy ar_pipeline stub_backend
uv run pytest -q
```
All must pass (32 tests).

```bash
git add -A
git commit -m "chore: add ruff, mypy, and CI workflow"
```

---

### Task 2: Graph data types and the `GraphClient` protocol + fake

**Files:**
- Create: `ar_pipeline/ingest/__init__.py`
- Create: `ar_pipeline/ingest/types.py`
- Create: `ar_pipeline/ingest/client.py`
- Create: `tests/ingest/__init__.py`
- Create: `tests/ingest/fakes.py`
- Create: `tests/ingest/test_fakes.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ar_pipeline.ingest.types.GraphMessage` — frozen dataclass: `id: str`, `internet_message_id: str`, `sender_address: str`, `subject: str`, `received_at: datetime`, `body_html: str`, `body_text: str`, `has_attachments: bool`, `removed: bool = False`.
  - `ar_pipeline.ingest.types.GraphAttachment` — frozen dataclass: `name: str`, `content_type: str`, `size: int`, `content: bytes`.
  - `ar_pipeline.ingest.types.DeltaResult` — frozen dataclass: `messages: list[GraphMessage]`, `delta_link: str`.
  - `ar_pipeline.ingest.client.GraphClient` — `Protocol` with:
    - `fetch_delta(self, delta_link: str | None) -> DeltaResult`
    - `download_attachments(self, message_id: str) -> list[GraphAttachment]`
  - `ar_pipeline.ingest.client.DeltaExpired(Exception)` — raised when a stored delta link is rejected (HTTP 410); the poller catches it and does a full resync.
  - `tests/ingest/fakes.FakeGraphClient` — in-memory implementation of `GraphClient`.

- [ ] **Step 1: Write the failing test**

`tests/ingest/test_fakes.py`:
```python
from datetime import datetime, timezone

import pytest

from ar_pipeline.ingest.client import DeltaExpired
from ar_pipeline.ingest.types import GraphAttachment, GraphMessage
from tests.ingest.fakes import FakeGraphClient


def _msg(mid: str, imid: str) -> GraphMessage:
    return GraphMessage(
        id=mid,
        internet_message_id=imid,
        sender_address="ap@vendor.com",
        subject="Remittance",
        received_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        body_html="<p>hi</p>",
        body_text="hi",
        has_attachments=False,
    )


def test_fake_returns_full_batch_when_delta_link_is_none():
    fake = FakeGraphClient(messages=[_msg("m1", "<a@v.com>"), _msg("m2", "<b@v.com>")])
    result = fake.fetch_delta(None)
    assert [m.id for m in result.messages] == ["m1", "m2"]
    assert result.delta_link  # non-empty token to persist


def test_fake_returns_only_new_messages_on_subsequent_delta():
    fake = FakeGraphClient(messages=[_msg("m1", "<a@v.com>")])
    first = fake.fetch_delta(None)
    fake.add_message(_msg("m2", "<b@v.com>"))
    second = fake.fetch_delta(first.delta_link)
    assert [m.id for m in second.messages] == ["m2"]


def test_fake_raises_delta_expired_for_unknown_link():
    fake = FakeGraphClient(messages=[])
    with pytest.raises(DeltaExpired):
        fake.fetch_delta("bogus-link")


def test_fake_download_attachments():
    att = GraphAttachment(name="s.xlsx", content_type="application/vnd...", size=3, content=b"abc")
    fake = FakeGraphClient(messages=[_msg("m1", "<a@v.com>")], attachments={"m1": [att]})
    got = fake.download_attachments("m1")
    assert got == [att]
    assert fake.download_attachments("m2") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/ingest/test_fakes.py -v`
Expected: FAIL — `ModuleNotFoundError: ar_pipeline.ingest.types`

- [ ] **Step 3: Implement the types and protocol**

`ar_pipeline/ingest/__init__.py`: empty.

`ar_pipeline/ingest/types.py`:
```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class GraphMessage:
    id: str
    internet_message_id: str
    sender_address: str
    subject: str
    received_at: datetime
    body_html: str
    body_text: str
    has_attachments: bool
    removed: bool = False


@dataclass(frozen=True, slots=True)
class GraphAttachment:
    name: str
    content_type: str
    size: int
    content: bytes


@dataclass(frozen=True, slots=True)
class DeltaResult:
    messages: list[GraphMessage]
    delta_link: str
```

`ar_pipeline/ingest/client.py`:
```python
from __future__ import annotations

from typing import Protocol

from ar_pipeline.ingest.types import DeltaResult, GraphAttachment


class DeltaExpired(Exception):
    """The stored delta link was rejected (HTTP 410). Caller must resync."""


class GraphClient(Protocol):
    def fetch_delta(self, delta_link: str | None) -> DeltaResult: ...

    def download_attachments(self, message_id: str) -> list[GraphAttachment]: ...
```

- [ ] **Step 4: Implement the fake**

`tests/ingest/__init__.py`: empty.

`tests/ingest/fakes.py`:
```python
from __future__ import annotations

from ar_pipeline.ingest.client import DeltaExpired, GraphClient
from ar_pipeline.ingest.types import DeltaResult, GraphAttachment, GraphMessage


class FakeGraphClient(GraphClient):
    """In-memory GraphClient. Each fetch_delta returns messages appended
    since the caller's delta_link; delta_link is the message count so far
    encoded as a string."""

    def __init__(
        self,
        messages: list[GraphMessage],
        attachments: dict[str, list[GraphAttachment]] | None = None,
    ) -> None:
        self._messages = list(messages)
        self._attachments = attachments or {}
        self._issued: set[str] = set()

    def add_message(self, message: GraphMessage) -> None:
        self._messages.append(message)

    def fetch_delta(self, delta_link: str | None) -> DeltaResult:
        if delta_link is None:
            start = 0
        elif delta_link in self._issued:
            start = int(delta_link.split(":")[1])
        else:
            raise DeltaExpired(delta_link)
        batch = self._messages[start:]
        new_link = f"delta:{len(self._messages)}"
        self._issued.add(new_link)
        return DeltaResult(messages=batch, delta_link=new_link)

    def download_attachments(self, message_id: str) -> list[GraphAttachment]:
        return list(self._attachments.get(message_id, []))
```

- [ ] **Step 5: Run tests, ruff, mypy**

```
uv run pytest tests/ingest/test_fakes.py -v
uv run ruff check
uv run mypy ar_pipeline stub_backend
```
Expected: 4 tests PASS; ruff + mypy clean.

- [ ] **Step 6: Commit**

```bash
git add ar_pipeline/ingest tests/ingest
git commit -m "feat: Graph client protocol, data types, and test fake"
```

---

### Task 3: MSAL app-only authentication

**Files:**
- Modify: `pyproject.toml` (add `msal`)
- Create: `ar_pipeline/ingest/auth.py`
- Create: `tests/ingest/test_auth.py`

**Interfaces:**
- Consumes: `ar_pipeline.config.get_settings`.
- Produces:
  - `ar_pipeline.ingest.auth.GraphAuth` — `__init__(self, tenant_id: str, client_id: str, client_secret: str)`; method `token(self) -> str` returns a valid bearer token (MSAL caches internally and refreshes near expiry).
  - `GraphAuth.from_settings() -> GraphAuth` — classmethod; raises `GraphNotConfigured` if any of tenant/client id/secret/mailbox is blank.
  - `ar_pipeline.ingest.auth.GraphNotConfigured(Exception)`.

- [ ] **Step 1: Add msal**

```bash
uv add msal
```

- [ ] **Step 2: Write the failing test**

`tests/ingest/test_auth.py`:
```python
from unittest.mock import MagicMock, patch

import pytest

from ar_pipeline.ingest.auth import GraphAuth, GraphNotConfigured


@patch("ar_pipeline.ingest.auth.msal.ConfidentialClientApplication")
def test_token_returns_access_token(mock_app_cls):
    app = mock_app_cls.return_value
    app.acquire_token_for_client.return_value = {"access_token": "tok-123"}
    auth = GraphAuth("tenant", "client", "secret")
    assert auth.token() == "tok-123"
    app.acquire_token_for_client.assert_called_once_with(
        scopes=["https://graph.microsoft.com/.default"]
    )


@patch("ar_pipeline.ingest.auth.msal.ConfidentialClientApplication")
def test_token_raises_on_error_response(mock_app_cls):
    app = mock_app_cls.return_value
    app.acquire_token_for_client.return_value = {
        "error": "invalid_client",
        "error_description": "bad secret",
    }
    auth = GraphAuth("tenant", "client", "secret")
    with pytest.raises(RuntimeError, match="invalid_client"):
        auth.token()


def test_from_settings_raises_when_unconfigured(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    import ar_pipeline.config as config_module

    config_module.get_settings.cache_clear()
    with pytest.raises(GraphNotConfigured):
        GraphAuth.from_settings()
    config_module.get_settings.cache_clear()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/ingest/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: ar_pipeline.ingest.auth`

- [ ] **Step 4: Implement**

`ar_pipeline/ingest/auth.py`:
```python
from __future__ import annotations

import msal

from ar_pipeline.config import get_settings

_SCOPES = ["https://graph.microsoft.com/.default"]


class GraphNotConfigured(Exception):
    """Graph credentials / mailbox are not set in configuration."""


class GraphAuth:
    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        self._app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=client_secret,
        )

    @classmethod
    def from_settings(cls) -> GraphAuth:
        s = get_settings()
        secret = s.graph_client_secret.get_secret_value()
        if not (s.graph_tenant_id and s.graph_client_id and secret and s.shared_mailbox):
            raise GraphNotConfigured
        return cls(s.graph_tenant_id, s.graph_client_id, secret)

    def token(self) -> str:
        result = self._app.acquire_token_for_client(scopes=_SCOPES)
        if "access_token" not in result:
            raise RuntimeError(
                f"MSAL token error: {result.get('error')} "
                f"{result.get('error_description')}"
            )
        token: str = result["access_token"]
        return token
```

- [ ] **Step 5: Verify and commit**

```
uv run pytest tests/ingest/test_auth.py -v
uv run ruff check && uv run mypy ar_pipeline stub_backend
```
Expected: 3 tests PASS; clean.

```bash
git add ar_pipeline/ingest/auth.py tests/ingest/test_auth.py pyproject.toml uv.lock
git commit -m "feat: MSAL app-only Graph authentication"
```

---

### Task 4: Real `HttpGraphClient` (httpx)

**Files:**
- Modify: `ar_pipeline/ingest/client.py`
- Create: `tests/ingest/test_http_client.py`

**Interfaces:**
- Consumes: `ar_pipeline.ingest.auth.GraphAuth`, `ar_pipeline.ingest.types.*`, `ar_pipeline.ingest.client.GraphClient` protocol + `DeltaExpired`.
- Produces:
  - `ar_pipeline.ingest.client.HttpGraphClient` — `__init__(self, auth: GraphAuth, mailbox: str, *, http: httpx.Client | None = None, max_retries: int = 3)`. Implements the `GraphClient` protocol.
  - `HttpGraphClient.from_settings() -> HttpGraphClient` — builds `GraphAuth.from_settings()` + `get_settings().shared_mailbox`.

**Graph API reference (verify exact shapes against Microsoft Learn — `docs.microsoft.com/graph/api/message-delta` and `/graph/api/message-list-attachments` — before finalising the parsing):**
- Delta: `GET https://graph.microsoft.com/v1.0/users/{mailbox}/mailFolders/inbox/messages/delta?$select=id,internetMessageId,from,subject,receivedDateTime,body,bodyPreview,hasAttachments`
  - First call uses that URL; subsequent calls use the full URL from the previous response's `@odata.deltaLink` (or `@odata.nextLink` while paging).
  - Response: `{ "value": [ <message>... ], "@odata.nextLink": "<url>" }` while more pages remain, then `{ "value": [...], "@odata.deltaLink": "<url>" }` on the last page. Follow `nextLink` until `deltaLink` appears; accumulate `value` across pages; return the final `deltaLink` as `DeltaResult.delta_link`.
  - A removed message: `{ "id": "...", "@removed": { "reason": "deleted" } }` — map to `GraphMessage(..., removed=True)` with best-effort/empty other fields (only `id` is guaranteed).
  - A 410 response (or an error body with code `"SyncStateNotFound"`) when sending a stored delta link → raise `DeltaExpired`.
- Message fields: `internetMessageId` (str), `from.emailAddress.address` (str), `subject` (str), `receivedDateTime` (ISO-8601 UTC `Z`), `body.contentType` (`"html"` or `"text"`) + `body.content` (str), `bodyPreview` (str), `hasAttachments` (bool). Populate `body_html` from `body.content` when `contentType == "html"` else `""`; `body_text` from `body.content` when `contentType == "text"` else fall back to `bodyPreview`.
- Attachments: `GET .../users/{mailbox}/messages/{id}/attachments`
  - Response `value` items with `@odata.type == "#microsoft.graph.fileAttachment"` have `name`, `contentType`, `size` (int), `contentBytes` (base64 str). Decode `contentBytes` → `bytes`. Skip non-file attachments (`itemAttachment`, `referenceAttachment`) for now.
  - `value` may paginate via `@odata.nextLink` — follow it.

- [ ] **Step 1: Write the failing tests**

`tests/ingest/test_http_client.py`:
```python
import base64
import json
from unittest.mock import MagicMock

import httpx
import pytest

from ar_pipeline.ingest.client import DeltaExpired, HttpGraphClient


class _Auth:
    def token(self) -> str:
        return "tok"


def _client(handler) -> HttpGraphClient:
    transport = httpx.MockTransport(handler)
    return HttpGraphClient(
        _Auth(),  # type: ignore[arg-type]
        "ar@company.com",
        http=httpx.Client(transport=transport, base_url="https://graph.microsoft.com/v1.0"),
    )


def _message(mid, imid, *, html="<p>x</p>"):
    return {
        "id": mid,
        "internetMessageId": imid,
        "from": {"emailAddress": {"address": "ap@vendor.com"}},
        "subject": "Remittance",
        "receivedDateTime": "2026-09-09T10:00:00Z",
        "body": {"contentType": "html", "content": html},
        "bodyPreview": "x",
        "hasAttachments": True,
    }


def test_fetch_delta_follows_pages_and_returns_delta_link():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "nextpage" in str(request.url):
            return httpx.Response(200, json={
                "value": [_message("m2", "<b@v.com>")],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/DELTA",
            })
        return httpx.Response(200, json={
            "value": [_message("m1", "<a@v.com>")],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/nextpage",
        })

    result = _client(handler).fetch_delta(None)
    assert [m.id for m in result.messages] == ["m1", "m2"]
    assert result.delta_link == "https://graph.microsoft.com/v1.0/DELTA"
    assert len(calls) == 2


def test_fetch_delta_maps_removed_messages():
    def handler(request):
        return httpx.Response(200, json={
            "value": [{"id": "gone", "@removed": {"reason": "deleted"}}],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/DELTA",
        })

    result = _client(handler).fetch_delta(None)
    assert result.messages[0].removed is True
    assert result.messages[0].id == "gone"


def test_fetch_delta_raises_delta_expired_on_410():
    def handler(request):
        return httpx.Response(410, json={"error": {"code": "SyncStateNotFound"}})

    with pytest.raises(DeltaExpired):
        _client(handler).fetch_delta("https://graph.microsoft.com/v1.0/OLD")


def test_fetch_delta_retries_on_429_then_succeeds():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={
            "value": [],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/DELTA",
        })

    result = _client(handler).fetch_delta(None)
    assert result.delta_link.endswith("/DELTA")
    assert state["n"] == 2


def test_download_attachments_decodes_file_attachments():
    def handler(request):
        return httpx.Response(200, json={"value": [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": "s.xlsx", "contentType": "application/vnd.ms-excel",
                "size": 3, "contentBytes": base64.b64encode(b"abc").decode(),
            },
            {"@odata.type": "#microsoft.graph.itemAttachment", "name": "skip"},
        ]})

    got = _client(handler).download_attachments("m1")
    assert len(got) == 1
    assert got[0].name == "s.xlsx"
    assert got[0].content == b"abc"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ingest/test_http_client.py -v`
Expected: FAIL — `ImportError: cannot import name 'HttpGraphClient'`

- [ ] **Step 3: Implement `HttpGraphClient`**

Append to `ar_pipeline/ingest/client.py`. Key points:
- Constructor builds a default `httpx.Client(base_url="https://graph.microsoft.com/v1.0", timeout=30)` if `http` is None.
- A private `_get(self, url: str) -> dict` that: sets `Authorization: Bearer {auth.token()}`; on `429` reads `Retry-After` (seconds, default 2), sleeps (`time.sleep`), retries up to `max_retries`; on `410` raises `DeltaExpired`; on other 4xx/5xx raises `httpx.HTTPStatusError` via `raise_for_status()`; returns `response.json()`.
- `fetch_delta`: build the first URL (`f"/users/{self._mailbox}/mailFolders/inbox/messages/delta?$select=..."`) if `delta_link is None`, else use `delta_link` verbatim. Loop: `_get(url)` → extend a `messages` list by parsing each `value` item; if `@odata.nextLink` present, set `url = that` and continue; else read `@odata.deltaLink`, break. Return `DeltaResult(messages, delta_link)`.
- `_parse_message(item: dict) -> GraphMessage`: handle `@removed` (return with `removed=True`, `id` only, empty strings, `received_at = datetime.now(tz=UTC)` placeholder, `has_attachments=False`). Otherwise parse per the field map above; `received_at` via `datetime.fromisoformat(value.replace("Z", "+00:00"))`.
- `download_attachments`: `_get(f"/users/{self._mailbox}/messages/{message_id}/attachments")`, follow `@odata.nextLink`, keep only `@odata.type == "#microsoft.graph.fileAttachment"`, `base64.b64decode(contentBytes)`.
- `from_settings()`: `HttpGraphClient(GraphAuth.from_settings(), get_settings().shared_mailbox)`.

Add `import base64`, `import time`, `from datetime import UTC, datetime`, `import httpx` at the top of `client.py` as needed. Keep the `GraphClient` protocol and `DeltaExpired` where they are.

- [ ] **Step 4: Verify**

```
uv run pytest tests/ingest/test_http_client.py -v
uv run ruff check && uv run mypy ar_pipeline stub_backend
```
Expected: 5 tests PASS; clean. Then full suite `uv run pytest -q`.

- [ ] **Step 5: Commit**

```bash
git add ar_pipeline/ingest/client.py tests/ingest/test_http_client.py
git commit -m "feat: httpx Graph client (delta paging, 429 retry, 410 resync, attachments)"
```

---

### Task 5: The poller

**Files:**
- Create: `ar_pipeline/ingest/poller.py`
- Create: `tests/ingest/test_poller.py`

**Interfaces:**
- Consumes: `GraphClient` protocol + `DeltaExpired`, `GraphMessage`, `ar_pipeline.storage.BlobStore`, `ar_pipeline.db.models` (`Email`, `Attachment`, `PollState`), a SQLAlchemy `Session`.
- Produces:
  - `ar_pipeline.ingest.poller.PollStats` — frozen dataclass: `new_emails: int`, `attachments: int`, `duplicates: int`, `removed: int`, `resynced: bool`.
  - `ar_pipeline.ingest.poller.poll_once(graph: GraphClient, blob_store: BlobStore, session: Session) -> PollStats` — does one full poll cycle inside the given session (caller commits, or use `get_session()`).
  - `ar_pipeline.ingest.poller.sender_domain(address: str) -> str` — helper (`"a@b.com" -> "b.com"`, `"" -> ""`).

Behaviour:
1. Load the singleton `PollState` (id=1); create it if absent (`delta_token=None`).
2. `try: result = graph.fetch_delta(state.delta_token)` — on `DeltaExpired`: set `state.delta_token = None`, `result = graph.fetch_delta(None)`, `resynced = True`.
3. For each `message` in `result.messages`:
   - if `message.removed`: `removed += 1`; continue (we never delete AR rows).
   - if an `Email` with that `internet_message_id` exists: `duplicates += 1`; continue.
   - insert `Email(internet_message_id=..., sender_address=message.sender_address, sender_domain=sender_domain(message.sender_address), subject=..., received_at=..., body_html=..., body_text=..., status="new")`; `session.flush()` to get `email.id`. Guard the flush with a `try/except IntegrityError` + `session.rollback()`-to-savepoint → treat as duplicate (covers a race / a message seen twice in one batch).
   - if `message.has_attachments`: for each `GraphAttachment` from `graph.download_attachments(message.id)`: `key = f"{email.id}/{att.name}"`; `blob_store.put(key, att.content)` → url; `Attachment(email_id=email.id, filename=att.name, content_type=att.content_type, size=att.size, blob_url=url, sha256=blob_store.sha256(att.content))`; `attachments += 1`.
   - `new_emails += 1`.
4. `state.delta_token = result.delta_link`; `state.last_poll_at = datetime.now(UTC)`.
5. Return `PollStats`.

- [ ] **Step 1: Write the failing tests**

`tests/ingest/test_poller.py`:
```python
from datetime import datetime, timezone

from sqlalchemy import select

from ar_pipeline.db.models import Attachment, Email, PollState
from ar_pipeline.ingest.poller import PollStats, poll_once, sender_domain
from ar_pipeline.ingest.types import GraphAttachment, GraphMessage
from ar_pipeline.storage import LocalBlobStore
from tests.ingest.fakes import FakeGraphClient


def _msg(mid, imid, *, has_att=False, removed=False):
    return GraphMessage(
        id=mid, internet_message_id=imid, sender_address="ap@vendor.com",
        subject="Remittance",
        received_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        body_html="<p>hi</p>", body_text="hi",
        has_attachments=has_att, removed=removed,
    )


def test_sender_domain():
    assert sender_domain("ap@Vendor.com") == "vendor.com"
    assert sender_domain("garbage") == ""
    assert sender_domain("") == ""


def test_poll_inserts_new_emails_and_persists_delta_token(db_session, tmp_path):
    graph = FakeGraphClient(messages=[_msg("m1", "<a@v.com>"), _msg("m2", "<b@v.com>")])
    stats = poll_once(graph, LocalBlobStore(str(tmp_path)), db_session)
    db_session.flush()

    assert stats.new_emails == 2
    assert db_session.scalars(select(Email.internet_message_id).order_by(Email.internet_message_id)).all() == [
        "<a@v.com>", "<b@v.com>",
    ]
    state = db_session.get(PollState, 1)
    assert state.delta_token == "delta:2"
    assert state.last_poll_at is not None


def test_poll_is_idempotent_across_runs(db_session, tmp_path):
    store = LocalBlobStore(str(tmp_path))
    graph = FakeGraphClient(messages=[_msg("m1", "<a@v.com>")])
    poll_once(graph, store, db_session)
    db_session.flush()
    graph.add_message(_msg("m2", "<b@v.com>"))
    stats = poll_once(graph, store, db_session)
    db_session.flush()

    assert stats.new_emails == 1
    assert db_session.scalar(select(Email).where(Email.internet_message_id == "<b@v.com>")) is not None
    assert db_session.scalars(select(Email)).unique().all().__len__() == 2


def test_poll_downloads_and_stores_attachments(db_session, tmp_path):
    att = GraphAttachment(name="s.xlsx", content_type="application/x", size=3, content=b"abc")
    graph = FakeGraphClient(
        messages=[_msg("m1", "<a@v.com>", has_att=True)], attachments={"m1": [att]}
    )
    store = LocalBlobStore(str(tmp_path))
    stats = poll_once(graph, store, db_session)
    db_session.flush()

    assert stats.attachments == 1
    row = db_session.scalar(select(Attachment))
    assert row.filename == "s.xlsx"
    assert row.sha256 == store.sha256(b"abc")
    assert store.get(f"{row.email_id}/s.xlsx") == b"abc"


def test_poll_skips_removed_messages(db_session, tmp_path):
    graph = FakeGraphClient(messages=[_msg("m1", "<a@v.com>", removed=True)])
    stats = poll_once(graph, LocalBlobStore(str(tmp_path)), db_session)
    assert stats.removed == 1
    assert stats.new_emails == 0


def test_poll_resyncs_on_delta_expired(db_session, tmp_path):
    graph = FakeGraphClient(messages=[_msg("m1", "<a@v.com>")])
    # seed a stale token
    db_session.add(PollState(id=1, delta_token="stale-token"))
    db_session.flush()
    stats = poll_once(graph, LocalBlobStore(str(tmp_path)), db_session)
    assert stats.resynced is True
    assert stats.new_emails == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ingest/test_poller.py -v`
Expected: FAIL — `ModuleNotFoundError: ar_pipeline.ingest.poller`

- [ ] **Step 3: Implement `ar_pipeline/ingest/poller.py`**

Follow the Behaviour spec above. Notes:
- `sender_domain`: `addr.split("@", 1)[1].lower()` guarded — return `""` if no `@` or empty.
- Dedup check: `session.scalar(select(Email.id).where(Email.internet_message_id == imid))`.
- Race guard on flush: wrap `session.flush()` in `try/except IntegrityError`; on hit, `session.rollback()` is wrong inside a savepoint test — instead use a `session.begin_nested()` around the insert+flush so a failure rolls back just that savepoint; count it as a duplicate.
- Use `from datetime import UTC, datetime` for `last_poll_at`.
- Do NOT call `session.commit()` — the caller owns the transaction (`get_session()` commits; the test's `db_session` rolls back).

- [ ] **Step 4: Verify**

```
uv run pytest tests/ingest/test_poller.py -v
uv run ruff check && uv run mypy ar_pipeline stub_backend
uv run pytest -q
```
Expected: 6 tests PASS; clean; full suite green.

- [ ] **Step 5: Commit**

```bash
git add ar_pipeline/ingest/poller.py tests/ingest/test_poller.py
git commit -m "feat: inbox poller (delta, dedup, attachments, resync)"
```

---

### Task 6: Wire `worker.poll_inbox()`

**Files:**
- Create: `ar_pipeline/ingest/service.py`
- Modify: `ar_pipeline/worker.py`
- Create: `tests/ingest/test_service.py`
- Modify: `tests/test_worker.py`

**Interfaces:**
- Consumes: `HttpGraphClient.from_settings`, `GraphNotConfigured`, `poll_once`, `get_blob_store`, `get_session`.
- Produces:
  - `ar_pipeline.ingest.service.run_poll() -> PollStats | None` — builds the real client from settings and calls `poll_once` inside `get_session()`; returns `None` (after logging) if `GraphNotConfigured`.
  - `ar_pipeline.worker.poll_inbox()` — now calls `ar_pipeline.ingest.service.run_poll()` and logs the stats; still returns `None`; still safe to run with no Graph config.

- [ ] **Step 1: Write the failing tests**

`tests/ingest/test_service.py`:
```python
import logging
from unittest.mock import patch

from ar_pipeline.ingest.auth import GraphNotConfigured
from ar_pipeline.ingest import service


def test_run_poll_returns_none_when_graph_not_configured(caplog):
    caplog.set_level(logging.INFO)
    with patch(
        "ar_pipeline.ingest.service.HttpGraphClient.from_settings",
        side_effect=GraphNotConfigured,
    ):
        assert service.run_poll() is None
    assert "not configured" in caplog.text.lower()


def test_run_poll_invokes_poll_once(monkeypatch):
    from ar_pipeline.ingest.poller import PollStats

    calls = {}

    class _FakeClient:
        pass

    monkeypatch.setattr(
        "ar_pipeline.ingest.service.HttpGraphClient.from_settings",
        classmethod(lambda cls: _FakeClient()),
    )
    monkeypatch.setattr(
        "ar_pipeline.ingest.service.get_blob_store", lambda: object()
    )

    def _fake_poll_once(graph, blob, session):
        calls["hit"] = True
        return PollStats(new_emails=1, attachments=0, duplicates=0, removed=0, resynced=False)

    monkeypatch.setattr("ar_pipeline.ingest.service.poll_once", _fake_poll_once)

    class _Ctx:
        def __enter__(self): return "session"
        def __exit__(self, *a): return False

    monkeypatch.setattr("ar_pipeline.ingest.service.get_session", lambda: _Ctx())

    stats = service.run_poll()
    assert calls["hit"] is True
    assert stats.new_emails == 1
```

Update `tests/test_worker.py::test_noop_jobs_return_none` (or add a new test) so `poll_inbox` is patched: assert `poll_inbox()` calls `ar_pipeline.ingest.service.run_poll` and returns `None`. Keep `advance_pipeline` / `run_deliveries` as no-ops.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/ingest/test_service.py -v`
Expected: FAIL — `ModuleNotFoundError: ar_pipeline.ingest.service`

- [ ] **Step 3: Implement**

`ar_pipeline/ingest/service.py`:
```python
from __future__ import annotations

import logging

from ar_pipeline.db.base import get_session
from ar_pipeline.ingest.auth import GraphNotConfigured
from ar_pipeline.ingest.client import HttpGraphClient
from ar_pipeline.ingest.poller import PollStats, poll_once
from ar_pipeline.storage import get_blob_store

log = logging.getLogger(__name__)


def run_poll() -> PollStats | None:
    try:
        graph = HttpGraphClient.from_settings()
    except GraphNotConfigured:
        log.info("poll_inbox: Graph not configured — skipping")
        return None
    blob_store = get_blob_store()
    with get_session() as session:
        stats = poll_once(graph, blob_store, session)
    log.info(
        "poll_inbox: %d new, %d attachments, %d dup, %d removed%s",
        stats.new_emails, stats.attachments, stats.duplicates, stats.removed,
        " (resynced)" if stats.resynced else "",
    )
    return stats
```

`ar_pipeline/worker.py` — replace the body of `poll_inbox`:
```python
def poll_inbox() -> None:
    from ar_pipeline.ingest.service import run_poll

    run_poll()
    return None
```
(Import inside the function to keep `worker` import-light and avoid a cycle.)

- [ ] **Step 4: Verify**

```
uv run pytest tests/ingest/test_service.py tests/test_worker.py -v
uv run ruff check && uv run mypy ar_pipeline stub_backend
uv run pytest -q
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add ar_pipeline/ingest/service.py ar_pipeline/worker.py tests/ingest/test_service.py tests/test_worker.py
git commit -m "feat: wire poll_inbox to the Graph poller"
```

---

## Self-Review

**1. Spec coverage (ingest/ section of the spec):**
- `graph_client.py`: MSAL app-only auth (token cache + refresh) → Task 3 (`auth.py`); delta query + message detail + attachment download → Task 4 (`HttpGraphClient`). ✓
- `poller.py`: read delta token from `poll_state`; INSERT `email` (status=new); download attachments to blob (URL + sha256); save new delta token; dedup via `internet_message_id` unique constraint → Task 5. ✓
- Errors: 429/throttling → respect `Retry-After` (Task 4 `_get`); delta token 410 Gone → discard + full resync (Task 4 raises `DeltaExpired`, Task 5 catches). ✓
- `worker.poll_inbox()` becomes real → Task 6. ✓
- Deferred-from-Foundation: ruff + mypy + CI → Task 1. ✓

**2. Placeholder scan:** Task 4's implementation step is prose + a Graph-shape reference rather than a full code block — deliberate, because the exact Graph JSON must be verified against Microsoft Learn and the test file pins the behaviour precisely (5 tests covering paging, removed, 410, 429, attachments). Every other task has complete code. No "TBD"/"handle errors"/"similar to".

**3. Type consistency:** `GraphClient.fetch_delta(delta_link: str | None) -> DeltaResult` and `download_attachments(message_id: str) -> list[GraphAttachment]` are identical across the protocol (Task 2), the fake (Task 2), `HttpGraphClient` (Task 4), and the poller's consumption (Task 5). `DeltaExpired` raised in Tasks 2/4, caught in Task 5. `PollStats` fields (`new_emails, attachments, duplicates, removed, resynced`) defined in Task 5, consumed in Task 6. `sender_domain` defined + tested in Task 5. `GraphNotConfigured` raised in Task 3, caught in Task 6.

## Execution Handoff

Handled in chat after the plan is saved.
