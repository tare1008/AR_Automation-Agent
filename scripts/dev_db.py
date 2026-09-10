"""Start a local embedded PostgreSQL (pgserver) for development.

    uv run python scripts/dev_db.py          # print DATABASE_URL / TEST_DATABASE_URL
    uv run python scripts/dev_db.py serve     # ... and stay running until Ctrl-C

The bare form prints the URLs and exits — but pgserver stops the postmaster
once no process holds it, so for a demo (app + CLI in other terminals) run
``serve`` in its own terminal and leave it open. The data lives under
.pgdata/ and the socket URL is stable between runs; delete that directory
to reset.
"""

from __future__ import annotations

import pathlib
import signal
import sys

import pgserver

PGDATA = pathlib.Path(__file__).resolve().parent.parent / ".pgdata"


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


if __name__ == "__main__":
    srv = ensure_server()
    print("DATABASE_URL=" + uri_for(srv, "ar_pipeline"), flush=True)
    print("TEST_DATABASE_URL=" + uri_for(srv, "ar_pipeline_test"), flush=True)
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        print("# pgserver up — leave this open (Ctrl-C to stop)", file=sys.stderr, flush=True)
        try:
            signal.pause()
        except KeyboardInterrupt:
            print("\n# stopping pgserver", file=sys.stderr)
