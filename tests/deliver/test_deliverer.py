from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ar_pipeline.db.models import Delivery, Email, Extraction
from ar_pipeline.deliver.backend_client import DeliveryResult
from ar_pipeline.deliver.deliverer import _BACKOFF, run_deliveries
from tests.deliver.fakes import FakeBackendClient

_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _approved_delivery(db_session, *, canonical=None, ext_status="approved") -> Delivery:
    email = Email(
        internet_message_id=f"m-{datetime.now().timestamp()}",
        sender_address="a@b.com",
        sender_domain="b.com",
        subject="s",
        received_at=_NOW,
        status="done",
    )
    db_session.add(email)
    db_session.flush()
    ext = Extraction(
        email_id=email.id,
        canonical=canonical if canonical is not None else {"envelope": {"extraction_id": "x"}},
        is_remittance=True,
        status=ext_status,
    )
    db_session.add(ext)
    db_session.flush()
    d = Delivery(extraction_id=ext.id, status="pending", attempts=0)
    db_session.add(d)
    db_session.flush()
    return d


def test_ok_marks_delivered(db_session):
    d = _approved_delivery(db_session)
    stats = run_deliveries(db_session, FakeBackendClient(), now=_NOW)
    db_session.refresh(d)
    assert stats.delivered == 1
    assert d.status == "delivered" and d.delivered_at == _NOW and d.attempts == 1


def test_permanent_fail_marks_failed(db_session):
    d = _approved_delivery(db_session)
    fake = FakeBackendClient([DeliveryResult("permanent_fail", 400, "bad")])
    run_deliveries(db_session, fake, now=_NOW)
    db_session.refresh(d)
    assert d.status == "failed" and d.last_error is not None and "400" in d.last_error


def test_transient_fail_schedules_next_attempt(db_session):
    d = _approved_delivery(db_session)
    fake = FakeBackendClient([DeliveryResult("transient_fail", 503, "down")])
    stats = run_deliveries(db_session, fake, now=_NOW)
    db_session.refresh(d)
    assert stats.retrying == 1
    assert d.status == "pending"
    assert d.next_attempt_at == _NOW + _BACKOFF[0]  # attempts==1 -> _BACKOFF[0] == 1m


def test_transient_fail_gives_up_after_ladder(db_session):
    d = _approved_delivery(db_session)
    d.attempts = len(_BACKOFF)  # 5 -> this call is attempt 6 -> over the ladder
    db_session.flush()
    fake = FakeBackendClient([DeliveryResult("transient_fail", 503, "down")])
    run_deliveries(db_session, fake, now=_NOW)
    db_session.refresh(d)
    assert d.status == "failed"


def test_not_yet_due_is_skipped(db_session):
    d = _approved_delivery(db_session)
    d.next_attempt_at = _NOW + timedelta(hours=1)
    db_session.flush()
    stats = run_deliveries(db_session, FakeBackendClient(), now=_NOW)
    db_session.refresh(d)
    assert stats == type(stats)()  # all zero
    assert d.status == "pending" and d.attempts == 0


def test_superseded_extraction_is_failed_not_sent(db_session):
    d = _approved_delivery(db_session, ext_status="superseded")
    fake = FakeBackendClient()
    run_deliveries(db_session, fake, now=_NOW)
    db_session.refresh(d)
    assert d.status == "failed"
    assert d.last_error is not None and "no longer approved" in d.last_error
    assert fake.calls == []  # never hit the backend


def test_one_bad_row_does_not_block_the_batch(db_session):
    _approved_delivery(db_session)
    _approved_delivery(db_session, canonical={"envelope": {"extraction_id": "y"}})

    # make the fake raise on the first call, succeed on the second
    class Boom(FakeBackendClient):
        def send(self, payload, idempotency_key):
            self.calls.append((payload, idempotency_key))
            if len(self.calls) == 1:
                raise RuntimeError("kaboom")
            return DeliveryResult("ok", 201, "")

    run_deliveries(db_session, Boom(), now=_NOW)
    statuses = {r.status for r in db_session.scalars(select(Delivery)).all()}
    assert statuses == {"failed", "delivered"}


def test_raised_exception_still_records_attempt(db_session):
    d = _approved_delivery(db_session)

    class Raiser:
        def send(self, payload, idempotency_key):
            raise RuntimeError("boom")

    stats = run_deliveries(db_session, Raiser(), now=_NOW)
    db_session.refresh(d)
    assert stats.failed == 1
    assert d.status == "failed"
    assert d.attempts == 1
    assert d.last_attempt_at == _NOW
    assert d.last_error is not None and "boom" in d.last_error
