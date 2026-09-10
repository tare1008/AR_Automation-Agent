"""Local embedded PostgreSQL (pgserver) for development and demos.

    uv run python scripts/dev_db.py              # print DATABASE_URL / TEST_DATABASE_URL
    uv run python scripts/dev_db.py --write-env  # ... and upsert them into ./.env
    uv run python scripts/dev_db.py migrate      # ... hold the server up and run alembic
    uv run python scripts/dev_db.py serve        # ... and stay running until Ctrl-C

pgserver only keeps the postmaster alive while a Python process holds its
handle, so anything that needs the database (migrations, the app, the CLI)
needs ``serve`` running in another terminal — or, for one-shot work like
migrations, use ``migrate`` which holds the server for exactly that call.
The data lives under .pgdata/ and the socket URL is stable between runs;
delete that directory to reset.

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
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        is_kv = "=" in line and not line.lstrip().startswith("#")
        key = line.split("=", 1)[0].strip() if is_kv else None
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return env_path


def _run_alembic_upgrade() -> int:
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    srv = ensure_server()  # held for the life of this process
    urls = {
        "DATABASE_URL": uri_for(srv, "ar_pipeline"),
        "TEST_DATABASE_URL": uri_for(srv, "ar_pipeline_test"),
    }
    for key, value in urls.items():
        print(f"{key}={value}", flush=True)

    if "--write-env" in args:
        path = write_env(urls)
        print(f"# wrote DATABASE_URL / TEST_DATABASE_URL to {path}", file=sys.stderr, flush=True)

    if "migrate" in args:
        # alembic env.py reads DATABASE_URL (via Settings / .env); the server
        # stays up because `srv` is still referenced here.
        write_env(urls)
        print("# alembic upgrade head", file=sys.stderr, flush=True)
        raise SystemExit(_run_alembic_upgrade())

    if "serve" in args:
        print("# pgserver up — leave this open (Ctrl-C to stop)", file=sys.stderr, flush=True)
        try:
            signal.pause()
        except KeyboardInterrupt:
            print("\n# stopping pgserver", file=sys.stderr)
