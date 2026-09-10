"""Start a local embedded PostgreSQL (pgserver) for development.

    uv run python scripts/dev_db.py              # print DATABASE_URL / TEST_DATABASE_URL
    uv run python scripts/dev_db.py --write-env  # ... and upsert them into ./.env
    uv run python scripts/dev_db.py serve        # ... and stay running until Ctrl-C

The bare form prints the URLs and exits — but pgserver stops the postmaster
once no process holds it, so for a demo (app + CLI in other terminals) run
``serve`` in its own terminal and leave it open. The data lives under
.pgdata/ and the socket URL is stable between runs; delete that directory
to reset.

pgserver bundles its own PostgreSQL binaries — no system Postgres or Docker
needed. It supports Linux and macOS (a Windows machine needs WSL2).
"""

from __future__ import annotations

import pathlib
import signal
import sys

import pgserver

ROOT = pathlib.Path(__file__).resolve().parent.parent
PGDATA = ROOT / ".pgdata"


def ensure_server() -> pgserver.PostgresServer:
    PGDATA.mkdir(exist_ok=True)
    server = pgserver.get_server(str(PGDATA))
    for name in ("ar_pipeline", "ar_pipeline_test"):
        exists = server.psql(f"SELECT 1 FROM pg_database WHERE datname = '{name}'").strip()
        if "1" not in exists:
            server.psql(f"CREATE DATABASE {name}")
    return server


def uri_for(server: pgserver.PostgresServer, database: str) -> str:
    return server.get_uri(database=database).replace("postgresql://", "postgresql+psycopg://", 1)


def write_env(updates: dict[str, str]) -> pathlib.Path:
    """Upsert ``KEY=value`` lines into ./.env, preserving everything else."""
    env_path = ROOT / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    env_path.write_text("\n".join(out) + "\n")
    return env_path


if __name__ == "__main__":
    srv = ensure_server()
    urls = {
        "DATABASE_URL": uri_for(srv, "ar_pipeline"),
        "TEST_DATABASE_URL": uri_for(srv, "ar_pipeline_test"),
    }
    for key, value in urls.items():
        print(f"{key}={value}", flush=True)

    if "--write-env" in sys.argv:
        path = write_env(urls)
        print(f"# wrote DATABASE_URL / TEST_DATABASE_URL to {path}", file=sys.stderr, flush=True)

    if "serve" in sys.argv[1:]:
        print("# pgserver up — leave this open (Ctrl-C to stop)", file=sys.stderr, flush=True)
        try:
            signal.pause()
        except KeyboardInterrupt:
            print("\n# stopping pgserver", file=sys.stderr)
