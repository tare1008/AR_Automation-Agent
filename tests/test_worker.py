from fastapi.testclient import TestClient

from ar_pipeline import worker
from ar_pipeline.main import app
from tests.normalize.llm_fake import FakeLLMClient


def test_scheduler_registers_three_jobs():
    sched = worker.build_scheduler()
    ids = {j.id for j in sched.get_jobs()}
    assert ids == {"poll_inbox", "advance_pipeline", "run_deliveries"}


def test_scheduler_job_defaults_applied():
    sched = worker.build_scheduler()
    assert sched._job_defaults["coalesce"] is True
    assert sched._job_defaults["max_instances"] == 1
    assert sched._job_defaults["misfire_grace_time"] == 300


def _settings_with(**over):
    from ar_pipeline.config import Settings

    base = Settings().model_dump()
    base.update(over)
    return Settings(**base)


def test_run_deliveries_noop_when_backend_url_unset(monkeypatch, caplog):
    import logging

    monkeypatch.setattr("ar_pipeline.config.get_settings", lambda: _settings_with(backend_url=""))
    caplog.set_level(logging.INFO)
    worker.run_deliveries()
    assert "backend_url not set" in caplog.text


def test_run_deliveries_calls_deliverer(monkeypatch, caplog):
    import logging

    from ar_pipeline.deliver.deliverer import DeliveryStats

    calls = []

    def fake_run(session, backend_client, *, batch=20, now=None):
        calls.append((session, backend_client))
        return DeliveryStats(delivered=2, failed=1, retrying=3)

    monkeypatch.setattr(
        "ar_pipeline.config.get_settings", lambda: _settings_with(backend_url="https://b/api")
    )
    monkeypatch.setattr("ar_pipeline.deliver.deliverer.run_deliveries", fake_run)
    monkeypatch.setattr("ar_pipeline.deliver.backend_client.get_backend_client", lambda: object())
    caplog.set_level(logging.INFO)
    worker.run_deliveries()
    assert len(calls) == 1
    assert "2 delivered, 1 failed, 3 retrying" in caplog.text


def test_advance_pipeline_calls_advance_once(monkeypatch, caplog):
    import logging

    from ar_pipeline.pipeline.advance import AdvanceStats

    calls: list[tuple] = []

    def fake_advance_once(session, blob_store, vision_extractor, llm_client, *, batch=20):
        calls.append((session, blob_store, vision_extractor, llm_client))
        return AdvanceStats(classified=2, extracted=1, normalized=3, errored=0)

    monkeypatch.setattr("ar_pipeline.pipeline.advance.advance_once", fake_advance_once)
    # don't build the real AnthropicLLMClient here -- swap in the fake.
    monkeypatch.setattr("ar_pipeline.normalize.llm_client.get_llm_client", lambda: FakeLLMClient())

    caplog.set_level(logging.INFO)
    worker.advance_pipeline()  # returns None by signature
    assert len(calls) == 1
    assert calls[0][3] is not None  # an llm client was built and passed through
    assert "2 classified, 1 extracted, 3 normalized, 0 errored" in caplog.text


def test_poll_inbox_calls_run_poll():
    from unittest.mock import patch

    with patch("ar_pipeline.ingest.service.run_poll") as run_poll:
        worker.poll_inbox()
    run_poll.assert_called_once_with()


def test_healthz_and_lifespan_starts_scheduler():
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert app.state.scheduler.running
    # after context exit, scheduler is shut down
    assert not app.state.scheduler.running
