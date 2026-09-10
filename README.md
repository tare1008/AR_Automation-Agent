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
