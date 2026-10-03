"""Payment keys, duplicates and history for freshly normalized extractions (spec §3)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ar_pipeline.config import get_settings
from ar_pipeline.db.models import EmailMessage, Extraction
from ar_pipeline.ledger.money import format_money
from ar_pipeline.threads.memory import KEY_LIVE
from ar_pipeline.threads.references import canonical_total, payment_key_for

DUPLICATE_TOLERANCE = Decimal("1.00")

# flag prefixes: written here and in threads.backfill, matched by review.save_edits
FLAG_REFERENCE = "header: reference "
FLAG_POSSIBLE_DUPLICATE = "header: possible duplicate"
FLAG_REJECTED_BEFORE = "header: rejected before"
FLAG_HISTORICAL = "header: historical —"
KEY_FLAG_PREFIXES = (FLAG_REFERENCE, FLAG_POSSIBLE_DUPLICATE, FLAG_REJECTED_BEFORE)


def _flag(row: Extraction, flag: str) -> None:
    # assign a new list so the MutableList column tracks the change
    row.validation_flags = list(row.validation_flags or []) + [flag]


def lock_payment_key(session: Session, key: str) -> None:
    """Serialise everyone checking or claiming ``key`` until this transaction ends."""
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})


def assign_payment_key(session: Session, row: Extraction) -> None:
    found = payment_key_for(row.canonical or {})
    if found is None:
        return
    key, strength = found
    total = canonical_total(row.canonical or {})
    if strength == "weak":
        other = session.scalar(
            select(Extraction)
            .where(
                Extraction.payment_key == key,
                Extraction.status.in_(KEY_LIVE),
                Extraction.id != row.id,
            )
            .order_by(Extraction.id)
            .limit(1)
        )
        row.payment_key, row.payment_key_strength = key, "weak"
        if other is not None:
            _flag(
                row,
                f"{FLAG_POSSIBLE_DUPLICATE} of payment {other.id} (same payer, amount and date)",
            )
        session.flush()
        return

    # lock BEFORE looking up, so two simultaneous arrivals can't both pass
    lock_payment_key(session, key)
    other = session.scalar(
        select(Extraction).where(
            Extraction.payment_key == key,
            Extraction.payment_key_strength == "strong",
            Extraction.status.in_(KEY_LIVE),
            Extraction.id != row.id,
        )
    )
    if other is None:
        rejected = session.scalar(
            select(Extraction)
            .where(
                Extraction.payment_key == key,
                Extraction.status == "rejected",
                Extraction.id != row.id,
            )
            .order_by(Extraction.reviewed_at.desc().nulls_last())
            .limit(1)
        )
        row.payment_key, row.payment_key_strength = key, "strong"
        if rejected is not None and rejected.reviewed_at is not None:
            _flag(
                row,
                f"{FLAG_REJECTED_BEFORE} on {rejected.reviewed_at:%d %b %Y} by "
                f"{rejected.reviewed_by}: {rejected.reject_reason}",
            )
        session.flush()
        return

    other_total = canonical_total(other.canonical or {})
    row.duplicate_of_id = other.id
    if (
        total is not None
        and other_total is not None
        and abs(total - other_total) <= DUPLICATE_TOLERANCE
    ):
        row.status = "duplicate"
    else:
        currency = ((other.canonical or {}).get("header") or {}).get("currency") or "INR"
        shown = format_money(other_total, currency) if other_total is not None else "another amount"
        _flag(
            row,
            f"{FLAG_REFERENCE}{key.split(':', 1)[1]} was already used for {shown} "
            f"(payment {other.id})",
        )
    session.flush()


def apply_history(
    session: Session,
    row: Extraction,
    message: EmailMessage | None,
    newest_content_position: int | None,
) -> None:
    if (
        message is not None
        and newest_content_position is not None
        and message.position > newest_content_position
    ):
        row.historical_reason = "earlier_message"
        _flag(
            row,
            f"{FLAG_HISTORICAL} earlier message in this thread; "
            "check it isn't already in your books",
        )
        session.flush()
        return
    go_live = get_settings().go_live_date
    if go_live is None:
        return
    raw = ((row.canonical or {}).get("header") or {}).get("payment_date")
    when: date | None = None
    try:
        when = date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        when = None
    if when is None and message is not None and message.sent_at is not None:
        when = message.sent_at.date()
    if when is not None and when < go_live:
        row.historical_reason = "before_go_live"
        _flag(
            row,
            f"{FLAG_HISTORICAL} dated {when:%d %b %Y}, before go-live "
            f"{go_live:%d %b %Y}; likely already recorded",
        )
        session.flush()
