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
