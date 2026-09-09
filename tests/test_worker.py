from fastapi.testclient import TestClient

from ar_pipeline import worker
from ar_pipeline.main import app


def test_scheduler_registers_three_jobs():
    sched = worker.build_scheduler()
    ids = {j.id for j in sched.get_jobs()}
    assert ids == {"poll_inbox", "advance_pipeline", "run_deliveries"}


def test_scheduler_job_defaults_applied():
    sched = worker.build_scheduler()
    assert sched._job_defaults["coalesce"] is True
    assert sched._job_defaults["max_instances"] == 1
    assert sched._job_defaults["misfire_grace_time"] == 300


def test_noop_jobs_return_none(caplog):
    import logging

    caplog.set_level(logging.INFO)
    assert worker.advance_pipeline() is None
    assert worker.run_deliveries() is None
    assert "advance_pipeline: no-op" in caplog.text
    assert "run_deliveries: no-op" in caplog.text


def test_poll_inbox_calls_run_poll():
    from unittest.mock import patch

    with patch("ar_pipeline.ingest.service.run_poll") as run_poll:
        assert worker.poll_inbox() is None
    run_poll.assert_called_once_with()


def test_healthz_and_lifespan_starts_scheduler():
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert app.state.scheduler.running
    # after context exit, scheduler is shut down
    assert not app.state.scheduler.running
