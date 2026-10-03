"""Thread memory: has this message, or every reference it names, been recorded?"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import EmailMessage, Extraction

RECORDED_FOR_SEEN = ("pending_review", "approved", "already_recorded", "duplicate")
KEY_LIVE = ("pending_review", "approved", "already_recorded")


def is_recorded(ext: Extraction) -> bool:
    if ext.status in RECORDED_FOR_SEEN:
        return True
    return ext.status == "rejected" and not ext.is_remittance


def message_is_recorded(session: Session, message_id: uuid.UUID) -> bool:
    # superseded rows were replaced by a reprocess: they say nothing either way
    rows = list(
        session.scalars(
            select(Extraction).where(
                Extraction.email_message_id == message_id, Extraction.status != "superseded"
            )
        )
    )
    return bool(rows) and all(is_recorded(r) for r in rows)


def find_seen_by_fingerprint(
    session: Session, *, email_id: uuid.UUID, fingerprint: str
) -> EmailMessage | None:
    candidates = session.scalars(
        select(EmailMessage)
        .where(
            EmailMessage.fingerprint == fingerprint,
            EmailMessage.status == "new",
            EmailMessage.email_id != email_id,
        )
        .order_by(EmailMessage.created_at.asc(), EmailMessage.id.asc())
    )
    for cand in candidates:
        if message_is_recorded(session, cand.id):
            return cand
    return None


def all_references_recorded(session: Session, refs: set[str]) -> bool:
    if not refs:
        return False
    keys = {f"utr:{r}" for r in refs}
    found = set(
        session.scalars(
            select(Extraction.payment_key).where(
                Extraction.payment_key.in_(keys),
                Extraction.payment_key_strength == "strong",
                Extraction.status.in_(KEY_LIVE),
            )
        )
    )
    return found == keys
