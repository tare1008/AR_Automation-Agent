"""Shared post-review routing: approve an extraction and queue its delivery,
and mark an email done once nothing is left to review.

Both the human review path (`review/service.approve_extraction`) and the
auto-send path (`normalize/service.normalize_one`) call these, so the two
never drift.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Delivery, Email, Extraction

AUTO_REVIEWER = "auto"


def approve_and_queue(session: Session, extraction: Extraction, *, reviewed_by: str) -> None:
    """Approve `extraction` and insert a pending `delivery`. The caller has
    already checked the extraction is approvable."""
    extraction.status = "approved"
    extraction.reviewed_by = reviewed_by
    extraction.reviewed_at = func.now()
    canonical = extraction.canonical
    if isinstance(canonical, dict):
        env = canonical.get("envelope")
        if isinstance(env, dict):
            # top-level reassignment so MutableDict tracks it
            extraction.canonical["envelope"] = {**env, "reviewed_by": reviewed_by}
    session.add(Delivery(extraction_id=extraction.id, status="pending", next_attempt_at=func.now()))
    session.flush()


def settle_email(session: Session, email: Email) -> None:
    """`email` -> `done` once no extraction for it is still `pending_review`."""
    pending = session.scalar(
        select(func.count())
        .select_from(Extraction)
        .where(Extraction.email_id == email.id, Extraction.status == "pending_review")
    )
    if not pending:
        email.status = "done"
    session.flush()
