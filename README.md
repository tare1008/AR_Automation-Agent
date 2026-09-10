# AR Email Settlement Extraction Pipeline

## Setup

    ./scripts/setup      # uv sync, .env, embedded Postgres, migrations, demo defaults

Or by hand:

    uv sync
    cp .env.example .env
    uv run python scripts/dev_db.py migrate   # writes the DB URLs to .env + runs alembic

`pgserver` only keeps Postgres alive while a Python process holds it, so
`dev_db.py migrate` starts it, runs the migration, and stops — don't split
that into a separate `alembic` call.

No Docker or system Postgres — `pgserver` bundles its own PostgreSQL
binaries (Linux and macOS; a **Windows** machine needs WSL2 first, then
the steps above run unchanged inside it). The test suite starts its own
instance automatically.

## Test
    uv run pytest

## Run
    uv run uvicorn ar_pipeline.main:app --reload
    uv run uvicorn stub_backend.app:app --port 9000 --reload

Run a single app process only — the in-process scheduler is not safe under
`uvicorn --workers N`.

## Review UI

The reviewer web UI is mounted at `/review` on the main app.

Environment:

| Var | Meaning |
|---|---|
| `REVIEW_AUTH_SECRET` | shared login password (required — the app refuses to serve `/review` without it) |
| `REVIEW_SESSION_SECRET` | **required** — signs the session cookie; the app refuses to start the auth provider while it is the dev default |
| `REVIEW_COOKIE_SECURE` | set to `false` only for local plain-HTTP development (default `true`) |

Run locally:

```bash
uv run uvicorn ar_pipeline.main:app --reload
# open http://localhost:8000/review  — log in with any name + REVIEW_AUTH_SECRET
```

Auth is a single shared secret for the demo, behind an `AuthProvider`
protocol (`ar_pipeline/review/auth.py`). A real deployment implements that
protocol with Entra ID OIDC — no route changes.

## Operator / demo CLI

`python -m ar_pipeline` (also installed as `ar-pipeline`):

| Command | What it does |
|---|---|
| `ingest-eml FILE [FILE ...]` | Import settlement emails from `.eml` files as `status="new"` — the offline equivalent of a mailbox poll. Deduplicates on `Message-ID`; never touches the Graph delta cursor. |
| `tick [--repeat N]` | Run the pipeline-advance and delivery jobs once (or N times), synchronously — step a demo instead of waiting on the 60 s background scheduler. |
| `status` | Print emails / extractions / deliveries grouped by state. |

## Local demo (no Microsoft 365, no deployment)

`./scripts/setup` leaves `.env` ready for an **offline** demo — no API key:
`LLM_PROVIDER=stub`, a generated `REVIEW_SESSION_SECRET`, `REVIEW_AUTH_SECRET=demo`,
`REVIEW_COOKIE_SECURE=false`, `BACKEND_URL=http://localhost:9000`,
`AUTO_APPROVE_MIN_CONFIDENCE=0.9` (flag-free extractions at or above this
confidence skip review and deliver automatically — `0` reviews everything).
Nothing to edit for a first run.

**`LLM_PROVIDER=stub`** runs the whole pipeline with no API key and no
network: it regex-parses the raw text for amounts / a bank reference /
an invoice token and emits one payment at `confidence=0.15`. Every field
is a guess — the reviewer corrects it in the UI, which is the Phase-1
story. Image / scanned-PDF attachments get a "not transcribed — enter by
hand" placeholder. For real extraction, set `LLM_PROVIDER=anthropic` and
`ANTHROPIC_API_KEY=sk-ant-...` in `.env`.

```bash
# terminal 1 — keep the embedded Postgres up for the whole demo
uv run python scripts/dev_db.py serve

# terminal 2 — the fake backend
uv run uvicorn stub_backend.app:app --port 9000

# terminal 3 — the pipeline + review UI (one process only)
uv run uvicorn ar_pipeline.main:app --port 8000
```

Then, in terminal 4:

```bash
# seed the queue from the .eml files in the repo
uv run ar-pipeline ingest-eml tests/fixtures/emails/*.eml
#   ... or your own redacted client emails:
uv run ar-pipeline ingest-eml "samples/"*.eml

# step it through classify -> extract -> normalize (or just wait ~2 min for the scheduler)
uv run ar-pipeline tick --repeat 3
uv run ar-pipeline status
```

Open <http://localhost:8000/review>, log in with any name + password `demo`
— the **Journey**. Some emails are already **Auto-approved & delivered**
(flag-free, high confidence); others are **Awaiting review**. Click any
row's JSON link to see the canonical payload. Open
<http://localhost:9000/> to see what the backend received.

The rest are in the review queue — open one, correct anything in the form,
**Save & Approve**. Back in terminal 4:

```bash
uv run ar-pipeline tick          # runs the delivery job
uv run ar-pipeline status        # deliveries: delivered 1
curl -s localhost:9000/remittances/<extraction-id> | jq   # the payload the backend received
```

If a delivery fails (e.g. stub backend down), it shows on the review UI's
Errors tab with a **Resend** button.

### Sharing with a teammate

Give them the repo (it clones and runs — `pgserver` brings Postgres). Do
**not** send `.env` or the `samples/` emails through git: `.env` is
per-machine, and `samples/` is gitignored because it holds real client
PII. If a teammate needs the real sample emails, send those `.eml` files
directly (encrypted). The committed `tests/fixtures/emails/*.eml` are
enough for a full demo.
