"""Bring rows created before threads up to date (run once after migration 0005)."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Email, EmailMessage, Extraction, ExtractionSource
from ar_pipeline.threads.memory import KEY_LIVE
from ar_pipeline.threads.references import payment_key_for


def backfill(session: Session) -> tuple[int, int, int]:
    """Returns (messages created, keys assigned, conflicts flagged). Safe to re-run."""
    created = keyed = conflicts = 0
    have = set(session.scalars(select(EmailMessage.email_id).distinct()))
    for email in session.scalars(select(Email).order_by(Email.received_at.asc())).all():
        if email.id in have:
            continue
        msg = EmailMessage(
            email_id=email.id,
            position=0,
            sender=email.sender_address.lower(),
            sent_at=email.received_at,
            raw_header="",
            is_internal=False,
            carries_attachments=True,
            status="new",
        )
        session.add(msg)
        session.flush()
        session.execute(
            update(ExtractionSource)
            .where(ExtractionSource.email_id == email.id)
            .values(email_message_id=msg.id)
        )
        session.execute(
            update(Extraction)
            .where(Extraction.email_id == email.id)
            .values(email_message_id=msg.id)
        )
        created += 1
    rows = session.scalars(
        select(Extraction)
        .where(
            Extraction.payment_key.is_(None),
            Extraction.is_remittance.is_(True),
            Extraction.duplicate_of_id.is_(None),  # already flagged: keeps re-runs a no-op
        )
        .order_by(
            (Extraction.status != "approved").asc(),
            Extraction.reviewed_at.asc().nulls_last(),
            Extraction.created_at.asc(),
            Extraction.id.asc(),
        )
    ).all()
    for row in rows:
        found = payment_key_for(row.canonical or {})
        if found is None:
            continue
        key, strength = found
        # only live strong keys are bound by ux_extraction_payment_key
        clash = None
        if strength == "strong" and row.status in KEY_LIVE:
            clash = session.scalar(
                select(Extraction.id).where(
                    Extraction.payment_key == key,
                    Extraction.payment_key_strength == "strong",
                    Extraction.status.in_(KEY_LIVE),
                    Extraction.id != row.id,
                )
            )
        if clash is not None:
            row.duplicate_of_id = clash
            row.validation_flags = list(row.validation_flags or []) + [
                f"header: possible duplicate of payment {clash} (same reference)"
            ]
            conflicts += 1
        else:
            row.payment_key, row.payment_key_strength = key, strength
            keyed += 1
        session.flush()
    return created, keyed, conflicts
