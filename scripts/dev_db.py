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
        exists = server.psql(f"SELECT 1 FROM pg_database WHERE datname = '{name}'").strip()
        if "1" not in exists:
            server.psql(f"CREATE DATABASE {name}")
    return server


def uri_for(server: pgserver.PostgresServer, database: str) -> str:
    return server.get_uri(database=database).replace("postgresql://", "postgresql+psycopg://", 1)


if __name__ == "__main__":
    srv = ensure_server()
    print("DATABASE_URL=" + uri_for(srv, "ar_pipeline"))
    print("TEST_DATABASE_URL=" + uri_for(srv, "ar_pipeline_test"))
