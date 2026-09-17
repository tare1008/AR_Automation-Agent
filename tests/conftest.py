import os
import pathlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Must be set before anything imports stub_backend.store, which reads this
# at module-import time to decide whether to persist to disk — an empty
# string means "don't", so test runs never write into the working tree.
os.environ["STUB_BACKEND_STORE_PATH"] = ""

PGDATA = pathlib.Path(__file__).resolve().parent.parent / ".pgdata"


@pytest.fixture(scope="session", autouse=True)
def _embedded_pg():
    import pgserver

    PGDATA.mkdir(exist_ok=True)
    server = pgserver.get_server(str(PGDATA))
    for name in ("ar_pipeline", "ar_pipeline_test"):
        exists = server.psql(f"SELECT 1 FROM pg_database WHERE datname = '{name}'").strip()
        if "1" not in exists:
            server.psql(f"CREATE DATABASE {name}")

    def uri(database: str) -> str:
        return server.get_uri(database=database).replace(
            "postgresql://", "postgresql+psycopg://", 1
        )

    os.environ["DATABASE_URL"] = uri("ar_pipeline")
    os.environ["TEST_DATABASE_URL"] = uri("ar_pipeline_test")
    os.environ["REVIEW_AUTH_SECRET"] = "test-shared-secret"
    os.environ["REVIEW_SESSION_SECRET"] = "test-session-signing-key"
    os.environ["REVIEW_COOKIE_SECURE"] = "false"
    # pin these to the real field defaults so a developer's local .env (e.g.
    # LLM_PROVIDER=stub for their own demo) can't silently change what a
    # test that relies on the default sees. Individual tests still
    # monkeypatch.setenv + get_settings.cache_clear() to opt into a
    # non-default value for the duration of that one test.
    os.environ["LLM_PROVIDER"] = "anthropic"
    os.environ["AUTO_APPROVE_MIN_CONFIDENCE"] = "0"
    os.environ["MAILBOX_PROVIDER"] = "graph"
    os.environ["BACKEND_URL"] = ""

    from ar_pipeline.config import get_settings
    from ar_pipeline.db.base import reset_engine

    get_settings.cache_clear()
    reset_engine()

    yield server
    # leave the server running for reuse across local runs; pgserver
    # reference-counts and cleans up when no processes remain.


@pytest.fixture(scope="session")
def _test_engine(_embedded_pg):
    from ar_pipeline.config import get_settings
    from ar_pipeline.db import models  # noqa: F401  (register mappers)
    from ar_pipeline.db.base import Base

    engine = create_engine(get_settings().test_database_url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(_test_engine):
    connection = _test_engine.connect()
    trans = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()
