"""Walk due ``delivery`` rows and POST them to the backend.

Same transaction discipline as ``pipeline/advance.py``: one
``begin_nested()`` savepoint + ``commit()`` per row, so a slow backend call
never pins one transaction across the batch, and a poison row fails only
itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Delivery, Extraction
from ar_pipeline.deliver.backend_client import DeliveryResult

_BACKOFF: list[timedelta] = [
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=6),
]

_ERR_CAP = 1000


class SendClient(Protocol):
    def send(self, payload: dict, idempotency_key: str) -> DeliveryResult: ...


@dataclass(frozen=True)
class DeliveryStats:
    delivered: int = 0
    failed: int = 0
    retrying: int = 0


def _cap(s: str) -> str:
    return s if len(s) <= _ERR_CAP else s[: _ERR_CAP - 1] + "…"


def run_deliveries(
    session: Session,
    backend_client: SendClient,
    *,
    batch: int = 20,
    now: datetime | None = None,
) -> DeliveryStats:
    cutoff = now or datetime.now(tz=UTC)
    rows = list(
        session.scalars(
            select(Delivery)
            .where(
                Delivery.status == "pending",
                (Delivery.next_attempt_at.is_(None)) | (Delivery.next_attempt_at <= cutoff),
            )
            .order_by(Delivery.next_attempt_at.asc().nulls_first(), Delivery.id)
            .limit(batch)
        )
    )

    delivered = failed = retrying = 0
    for row in rows:
        row_now = now if now is not None else datetime.now(tz=UTC)
        try:
            with session.begin_nested():
                outcome = _deliver_one(session, row, backend_client, row_now)
        except Exception as exc:  # savepoint rolled back
            row.status = "failed"
            row.last_error = _cap(f"{type(exc).__name__}: {exc}")
            row.attempts += 1
            row.last_attempt_at = row_now
            outcome = "failed"

        if outcome == "delivered":
            delivered += 1
        elif outcome == "failed":
            failed += 1
        else:
            retrying += 1
        session.commit()

    return DeliveryStats(delivered=delivered, failed=failed, retrying=retrying)


def _deliver_one(session: Session, row: Delivery, backend_client: SendClient, now: datetime) -> str:
    row.attempts += 1
    row.last_attempt_at = now

    ext = session.get(Extraction, row.extraction_id)
    if ext is None or ext.status != "approved":
        row.status = "failed"
        row.last_error = "extraction no longer approved"
        return "failed"

    result = backend_client.send(dict(ext.canonical), str(ext.id))

    if result.outcome == "ok":
        row.status = "delivered"
        row.delivered_at = now
        row.last_error = None
        return "delivered"

    row.last_error = _cap(f"{result.status_code}: {result.detail}")
    if result.outcome == "permanent_fail":
        row.status = "failed"
        return "failed"

    # transient_fail
    if row.attempts > len(_BACKOFF):
        row.status = "failed"
        return "failed"
    row.next_attempt_at = now + _BACKOFF[row.attempts - 1]
    return "retrying"
