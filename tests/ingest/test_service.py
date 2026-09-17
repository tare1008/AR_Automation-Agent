import logging
from unittest.mock import patch

import pytest

from ar_pipeline.ingest import service
from ar_pipeline.ingest.auth import GraphNotConfigured
from ar_pipeline.ingest.gmail_auth import GmailNotConfigured


@pytest.fixture(autouse=True)
def _clear_mailbox_client_cache():
    service._mailbox_client.cache_clear()
    yield
    service._mailbox_client.cache_clear()


def test_run_poll_returns_none_when_graph_not_configured(caplog):
    caplog.set_level(logging.INFO)
    # I2: the client is now built through the lru_cache wrapper _mailbox_client();
    # patch that instead of HttpGraphClient.from_settings.
    with patch(
        "ar_pipeline.ingest.service._mailbox_client",
        side_effect=GraphNotConfigured,
    ):
        assert service.run_poll() is None
    assert "not configured" in caplog.text.lower()


def test_run_poll_returns_none_when_gmail_not_configured(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("MAILBOX_PROVIDER", "gmail")
    import ar_pipeline.config as config_module

    config_module.get_settings.cache_clear()
    try:
        with patch(
            "ar_pipeline.ingest.service._mailbox_client",
            side_effect=GmailNotConfigured,
        ):
            assert service.run_poll() is None
        assert "not configured" in caplog.text.lower()
    finally:
        config_module.get_settings.cache_clear()


def test_run_poll_invokes_poll_once(monkeypatch):
    from ar_pipeline.ingest.poller import PollStats

    calls = {}

    class _FakeClient:
        pass

    monkeypatch.setattr(
        "ar_pipeline.ingest.service._mailbox_client",
        lambda: _FakeClient(),
    )
    monkeypatch.setattr("ar_pipeline.ingest.service.get_blob_store", lambda: object())

    def _fake_poll_once(graph, blob, session):
        calls["hit"] = True
        return PollStats(
            new_emails=1, attachments=0, duplicates=0, removed=0, failed=0, resynced=False
        )

    monkeypatch.setattr("ar_pipeline.ingest.service.poll_once", _fake_poll_once)

    class _Ctx:
        def __enter__(self):
            return "session"

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("ar_pipeline.ingest.service.get_session", lambda: _Ctx())

    stats = service.run_poll()
    assert calls["hit"] is True
    assert stats is not None
    assert stats.new_emails == 1


def test_mailbox_client_is_cached_across_calls(monkeypatch):
    built = {"n": 0}

    class _FakeClient:
        pass

    def _from_settings(cls):
        built["n"] += 1
        return _FakeClient()

    monkeypatch.setattr(
        "ar_pipeline.ingest.service.HttpGraphClient.from_settings",
        classmethod(_from_settings),
    )
    a = service._mailbox_client()
    b = service._mailbox_client()
    assert a is b
    assert built["n"] == 1


def test_mailbox_client_dispatches_to_gmail_when_selected(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("MAILBOX_PROVIDER", "gmail")
    import ar_pipeline.config as config_module

    config_module.get_settings.cache_clear()

    class _FakeGmailClient:
        pass

    def _from_settings(cls):
        return _FakeGmailClient()

    monkeypatch.setattr(
        "ar_pipeline.ingest.service.GmailClient.from_settings",
        classmethod(_from_settings),
    )
    try:
        assert isinstance(service._mailbox_client(), _FakeGmailClient)
    finally:
        config_module.get_settings.cache_clear()
