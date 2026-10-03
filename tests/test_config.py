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
    assert s.llm_model == "claude-opus-5"


def test_get_settings_is_cached(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg://u:p@localhost/db_test")
    config_module.get_settings.cache_clear()
    assert get_settings() is get_settings()


def test_client_lists_and_go_live(monkeypatch):
    import datetime

    import ar_pipeline.config as config_module

    monkeypatch.setenv("CLIENT_DOMAINS", " AdityaBirla.com, ,hindalco.com ")
    monkeypatch.setenv("CLIENT_NAMES", "Hindalco Industries")
    monkeypatch.setenv("GO_LIVE_DATE", "2026-10-15")
    config_module.get_settings.cache_clear()
    try:
        s = config_module.get_settings()
        assert s.client_domain_list() == ["adityabirla.com", "hindalco.com"]
        assert s.client_name_list() == ["Hindalco Industries"]
        assert s.go_live_date == datetime.date(2026, 10, 15)
    finally:
        config_module.get_settings.cache_clear()
