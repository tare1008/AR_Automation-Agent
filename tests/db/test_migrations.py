import os
import subprocess

import pytest


@pytest.fixture
def _migrations_db(_embedded_pg):
    """A dedicated throwaway database for exercising alembic up/down.

    Never the dev DB (``ar_pipeline``) and never ``ar_pipeline_test``
    (owned by the ``_test_engine`` fixture's ``create_all``).
    """
    server = _embedded_pg
    name = "ar_pipeline_migrations_test"
    exists = server.psql(
        f"SELECT 1 FROM pg_database WHERE datname = '{name}'"
    ).strip()
    if "1" not in exists:
        server.psql(f"CREATE DATABASE {name}")
    uri = server.get_uri(database=name).replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    yield uri


def _alembic(uri: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "ALEMBIC_DATABASE_URL": uri}
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        capture_output=True, text=True, env=env,
    )


def test_migrations_upgrade_and_downgrade(_migrations_db):
    up = _alembic(_migrations_db, "upgrade", "head")
    assert up.returncode == 0, up.stderr

    down = _alembic(_migrations_db, "downgrade", "base")
    assert down.returncode == 0, down.stderr

    again = _alembic(_migrations_db, "upgrade", "head")
    assert again.returncode == 0, again.stderr


def test_no_model_migration_drift(_migrations_db):
    up = _alembic(_migrations_db, "upgrade", "head")
    assert up.returncode == 0, up.stderr

    check = _alembic(_migrations_db, "check")
    assert check.returncode == 0, check.stdout + check.stderr
