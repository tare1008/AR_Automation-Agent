import pytest

import ar_pipeline.config as config_module
from ar_pipeline.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _restore_settings_cache():
    yield
    config_module.get_settings.cache_clear()
    from ar_pipeline.db.base import reset_engine

    reset_engine()


def test_settings_reads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://u:p@localhost/db_test")
    s = Settings()
    assert s.database_url.endswith("/db")
    assert s.poll_interval_seconds == 300
    assert s.llm_provider == "anthropic"


def test_get_settings_is_cached(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://u:p@localhost/db_test")
    config_module.get_settings.cache_clear()
    assert get_settings() is get_settings()
