"""Persist one email's normalized payments as ``extraction`` rows, per message.

``normalize_one`` groups an email's ``raw_extraction`` payloads by the thread
message they came from and, for every ``new`` message that has no live
extraction yet, runs ``normalize_email`` (LLM + deterministic validators) once
— oldest message first, so installments post in order. Each payment gets a
payment key (duplicates are stored as ``duplicate`` and never routed), history
flags, ledger checks and the auto-approve rule. A message whose normalization
raises is marked ``failed`` without blocking its siblings; emails classified
before threads (sources with no message) keep whole-email failure semantics.

It does NOT commit: the caller (``advance_once``) owns the transaction boundary
and commits per email. Pure at import — no DB, no network.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ar_pipeline.config import get_settings
from ar_pipeline.db.models import Email, EmailMessage, Extraction, ExtractionSource, RawExtraction
from ar_pipeline.ledger.adjustments import resolve_adjustments
from ar_pipeline.normalize.llm_client import LLMClient
from ar_pipeline.normalize.normalizer import normalize_email
from ar_pipeline.normalize.prompt import PROMPT_VERSION, is_truncated
from ar_pipeline.normalize.recheck import TRUNCATED_FLAG, context_flags
from ar_pipeline.pipeline.routing import AUTO_REVIEWER, approve_and_queue, settle_email
from ar_pipeline.schema.canonical import RemittancePayload
from ar_pipeline.tables.reader import read_by_table
from ar_pipeline.tables.totals import document_totals
from ar_pipeline.threads.dedupe import apply_history, assign_payment_key

__all__ = ["normalize_one"]

_LIVE = ("pending_review", "approved", "rejected", "already_recorded", "duplicate")
_LEGACY_ORDER = 10**6  # emails classified before threads: whole email, processed first


def _groups(session: Session, email: Email) -> list[tuple[EmailMessage | None, list[dict]]]:
    payloads: dict[uuid.UUID | None, list[dict]] = defaultdict(list)
    for message_id, payload in session.execute(
        select(ExtractionSource.email_message_id, RawExtraction.payload)
        .join(RawExtraction, RawExtraction.extraction_source_id == ExtractionSource.id)
        .where(ExtractionSource.email_id == email.id)
        # deterministic source order: ExtractionSource.id is a uuid4 PK, so
        # anything keyed on it reshuffles the prompt every run (I1).
        .order_by(ExtractionSource.kind, ExtractionSource.ref, RawExtraction.id)
    ):
        payloads[message_id].append(payload)
    messages = {
        m.id: m
        for m in session.scalars(select(EmailMessage).where(EmailMessage.email_id == email.id))
    }
    out: list[tuple[EmailMessage | None, list[dict]]] = []
    for message_id, raws in payloads.items():
        message = messages.get(message_id) if message_id else None
        if message is not None and message.status != "new":
            continue
        live = (
            select(func.count())
            .select_from(Extraction)
            .where(
                Extraction.email_id == email.id,
                Extraction.status.in_(_LIVE),
                Extraction.email_message_id.is_(None)
                if message_id is None
                else Extraction.email_message_id == message_id,
            )
        )
        if session.scalar(live):
            continue
        out.append((message, raws))
    # oldest first: the highest position is the oldest message; legacy group first
    out.sort(key=lambda g: -(g[0].position if g[0] is not None else _LEGACY_ORDER))
    return out


def _newest_content_position(session: Session, email: Email) -> int | None:
    return session.scalar(
        select(func.min(EmailMessage.position)).where(
            EmailMessage.email_id == email.id,
            EmailMessage.status.in_(("new", "seen", "failed")),
        )
    )


def _normalize_group(
    session: Session,
    email: Email,
    message: EmailMessage | None,
    raws: list[dict],
    llm_client: LLMClient,
) -> list[Extraction]:
    sender = (
        message.sender
        if message is not None and message.sender and "@" in message.sender
        else email.sender_address
    )
    model = get_settings().llm_model
    client_names = get_settings().client_name_list()
    read = read_by_table(
        session,
        email_id=str(email.id),
        sender=sender,
        subject=email.subject,
        raws=raws,
        llm_client=llm_client,
        client_names=client_names,
    )
    read_info: dict
    if read is not None:
        out, payments, read_info = read.output, read.payments, read.read_info
        truncated = bool(read_info.get("header_truncated"))
    else:
        out, payments = normalize_email(
            email_id=str(email.id),
            sender_address=sender,
            subject=email.subject,
            raw_extractions=raws,
            llm_client=llm_client,
            client_names=client_names,
        )
        texts = [str(r.get("text") or "") for r in raws]
        # the amount in words names one payment; a multi-payment read can't use it
        read_info = {
            "path": "ai",
            "mapping": None,
            "document_totals": document_totals({}, texts) if len(payments) == 1 else {},
        }
        truncated = is_truncated(sender, email.subject, raws)
    message_id = message.id if message is not None else None
    rows: list[Extraction] = []
    if payments:
        for payment in payments:
            flags = list(payment.validation_flags) + ([TRUNCATED_FLAG] if truncated else [])
            payload = resolve_adjustments(session, payment.payload)
            row = Extraction(
                email_id=email.id,
                email_message_id=message_id,
                canonical=payload.model_dump(mode="json"),
                confidence=payment.confidence,
                is_remittance=payment.is_remittance,
                validation_flags=flags,
                llm_model=model,
                prompt_version=PROMPT_VERSION,
                raw_llm_response=dict(payment.raw_llm_response),
                read_info=read_info,
                status="pending_review",
            )
            session.add(row)
            rows.append(row)
    else:
        if not out.is_remittance:
            flags = ["LLM: not a remittance"]
        elif out.notes:
            # normalize_email appends a "schema validation failed" note here when
            # every draft was dropped -- surface it rather than a misleading
            # "LLM returned no payments" (M-b).
            flags = [f"LLM: {out.notes}"]
        else:
            flags = ["LLM returned no payments"]
        session.add(
            Extraction(
                email_id=email.id,
                email_message_id=message_id,
                canonical={},
                confidence=Decimal("0"),
                is_remittance=out.is_remittance,
                validation_flags=flags,
                llm_model=model,
                prompt_version=PROMPT_VERSION,
                raw_llm_response=out.model_dump(mode="json"),
                status="pending_review",
            )
        )
    session.flush()
    # the normalizer minted a placeholder uuid for envelope.extraction_id
    # before these rows had a PK; make it authoritative now.
    for row in rows:
        env = row.canonical.get("envelope")
        if isinstance(env, dict):
            # top-level reassignment so MutableDict tracks the change
            row.canonical["envelope"] = {**env, "extraction_id": str(row.id)}
    session.flush()
    return rows


def _route(
    session: Session,
    message: EmailMessage | None,
    rows: list[Extraction],
    newest: int | None,
) -> None:
    threshold = get_settings().auto_approve_min_confidence
    for row in rows:
        if not (row.is_remittance and row.canonical):
            continue
        assign_payment_key(session, row)
        if row.status == "duplicate":
            continue  # never routed, delivered or posted
        apply_history(session, row, message, newest)
        # Ledger flags are computed per row, immediately before that row's
        # routing decision, so a payment approved earlier in this loop already
        # counts toward the invoice balance (spec §2 "Re-check at approval").
        payload = RemittancePayload.model_validate(row.canonical)
        row.validation_flags = list(row.validation_flags) + context_flags(session, row, payload)
        session.flush()
        if (
            threshold > 0
            and not row.validation_flags
            and row.confidence is not None
            and float(row.confidence) >= threshold
        ):
            approve_and_queue(session, row, reviewed_by=AUTO_REVIEWER)


def normalize_one(session: Session, email: Email, llm_client: LLMClient) -> int:
    """Normalize every new, not-yet-processed message of one email (oldest first).

    Returns the number of ``Extraction`` rows added. Sets ``email.status`` to
    ``done`` (via ``settle_email``) or ``review``, or ``error`` when every
    message failed; flushes, does not commit.
    """
    groups = _groups(session, email)
    if not groups:
        has_rows = session.scalar(
            select(func.count())
            .select_from(Extraction)
            .where(Extraction.email_id == email.id, Extraction.status != "superseded")
        )
        if has_rows:
            email.status = "review"
            settle_email(session, email)
            return 0
        email.status = "error"
        email.error_detail = "no raw extractions to normalize"
        session.flush()
        return 0

    newest = _newest_content_position(session, email)
    added = failed = 0
    for message, raws in groups:
        if message is None:  # legacy whole-email path keeps whole-email failure
            rows = _normalize_group(session, email, None, raws, llm_client)
            _route(session, None, rows, newest)
            added += max(len(rows), 1)
            continue
        try:
            with session.begin_nested():
                rows = _normalize_group(session, email, message, raws, llm_client)
                _route(session, message, rows, newest)
            added += max(len(rows), 1)
        except Exception as exc:  # noqa: BLE001 -- per-message isolation is the point
            message.status = "failed"
            message.error_detail = f"{type(exc).__name__}: {exc}"[:2000]
            failed += 1
            session.flush()

    if added == 0 and failed:
        email.status = "error"
        email.error_detail = f"{failed} message(s) failed — see Errors"
        session.flush()
        return 0
    email.status = "review"
    session.flush()
    settle_email(session, email)
    return added
