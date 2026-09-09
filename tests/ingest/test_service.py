import logging
from unittest.mock import patch

from ar_pipeline.ingest import service
from ar_pipeline.ingest.auth import GraphNotConfigured


def test_run_poll_returns_none_when_graph_not_configured(caplog):
    caplog.set_level(logging.INFO)
    with patch(
        "ar_pipeline.ingest.service.HttpGraphClient.from_settings",
        side_effect=GraphNotConfigured,
    ):
        assert service.run_poll() is None
    assert "not configured" in caplog.text.lower()


def test_run_poll_invokes_poll_once(monkeypatch):
    from ar_pipeline.ingest.poller import PollStats

    calls = {}

    class _FakeClient:
        pass

    monkeypatch.setattr(
        "ar_pipeline.ingest.service.HttpGraphClient.from_settings",
        classmethod(lambda cls: _FakeClient()),
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
    assert stats.new_emails == 1
