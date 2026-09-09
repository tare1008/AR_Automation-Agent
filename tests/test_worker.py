from httpx import ASGITransport, AsyncClient

from ar_pipeline import worker
from ar_pipeline.main import app


def test_scheduler_registers_three_jobs():
    sched = worker.build_scheduler()
    ids = {j.id for j in sched.get_jobs()}
    assert ids == {"poll_inbox", "advance_pipeline", "run_deliveries"}


def test_noop_jobs_return_none(caplog):
    import logging

    caplog.set_level(logging.INFO)
    assert worker.poll_inbox() is None
    assert worker.advance_pipeline() is None
    assert worker.run_deliveries() is None
    assert "poll_inbox: no-op" in caplog.text


async def test_healthz():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
