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

Four terminals. Everything reads `.env`, so create it once:

```bash
uv run python scripts/dev_db.py            # prints DATABASE_URL / TEST_DATABASE_URL
cat > .env <<'EOF'
DATABASE_URL=<paste from above>
TEST_DATABASE_URL=<paste from above>
BLOB_DIR=data/blob
BACKEND_URL=http://localhost:9000
REVIEW_AUTH_SECRET=demo-pass
REVIEW_SESSION_SECRET=<any long random string>
REVIEW_COOKIE_SECURE=false
ANTHROPIC_API_KEY=sk-ant-...
EOF
```

`ANTHROPIC_API_KEY` is required — normalization and image/scanned-PDF
extraction call the real API. Without it those emails land in `error`
(visible on the review UI's Errors tab).

```bash
# terminal 1 — keep the embedded Postgres up for the whole demo
uv run python scripts/dev_db.py serve

# terminal 2 — one-time schema
uv run alembic upgrade head

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

Open <http://localhost:8000/review>, log in with any name + `demo-pass`.
The extraction is in the queue — open it, correct anything in the form,
**Save & Approve**. Back in terminal 4:

```bash
uv run ar-pipeline tick          # runs the delivery job
uv run ar-pipeline status        # deliveries: delivered 1
curl -s localhost:9000/remittances/<extraction-id> | jq   # the payload the backend received
```

If a delivery fails (e.g. stub backend down), it shows on the review UI's
Errors tab with a **Resend** button.
