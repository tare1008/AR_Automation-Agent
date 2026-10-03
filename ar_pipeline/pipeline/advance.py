"""Advance emails one pipeline state at a time: ``new -> classified -> extracted -> review``.

``advance_once`` pulls a batch of pending emails and moves each forward exactly
one state. Every email is processed inside its own ``session.begin_nested()``
savepoint so a failing step poisons only that email. Extraction goes further:
each source is extracted inside its *own* nested savepoint, so a corrupt
attachment fails only its own source and its healthy siblings' ``RawExtraction``
rows survive (a manual retry then re-runs only the failed source).

``advance_once`` commits after every email (success or error): the caller's
session object is used but its transaction boundary is not relied upon, so a
blocking vision call never pins a connection across the whole batch.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from ar_pipeline.classify.classifier import (
    BODY_TEXT_MIN_CHARS,
    SourceSpec,
    classify_email,
    has_numeric_table_rows,
)
from ar_pipeline.config import get_settings
from ar_pipeline.db.models import (
    Attachment,
    Email,
    EmailMessage,
    ExtractionSource,
    RawExtraction,
)
from ar_pipeline.extract.base import EXTRACTOR_VERSION, ExtractedContent
from ar_pipeline.extract.body_text import extract_body_text
from ar_pipeline.extract.excel import extract_excel
from ar_pipeline.extract.html_table import extract_html_tables
from ar_pipeline.extract.pdf import extract_pdf
from ar_pipeline.extract.vision import VisionExtractor
from ar_pipeline.normalize.llm_client import LLMClient
from ar_pipeline.normalize.service import normalize_one
from ar_pipeline.storage import BlobStore, attachment_blob_key
from ar_pipeline.threads.memory import all_references_recorded, find_seen_by_fingerprint
from ar_pipeline.threads.references import find_references
from ar_pipeline.threads.splitter import MessagePart, split_email

logger = logging.getLogger(__name__)

_PENDING_STATUSES = ("new", "classified", "extracted")


@dataclass(frozen=True)
class AdvanceStats:
    classified: int = 0
    extracted: int = 0
    normalized: int = 0
    errored: int = 0


def advance_once(
    session: Session,
    blob_store: BlobStore,
    vision_extractor: VisionExtractor,
    llm_client: LLMClient,
    *,
    batch: int = 20,
) -> AdvanceStats:
    """Advance one batch of pending emails, committing after each one.

    The caller's ``session`` object is used but its transaction boundary is not
    relied upon: extraction is I/O-heavy (a blocking vision call per email) and
    holding one transaction open across the whole batch would pin a connection
    for minutes. ``batch=20`` is kept for now; a smaller batch may be wanted
    once real vision volume lands.
    """
    emails = list(
        session.scalars(
            select(Email)
            .where(Email.status.in_(_PENDING_STATUSES))
            .order_by(Email.received_at.asc())
            .limit(batch)
        )
    )

    classified = extracted = normalized = errored = 0
    for email in emails:
        try:
            with session.begin_nested():
                result = _step(session, email, blob_store, vision_extractor, llm_client)
        except Exception as exc:  # savepoint already rolled back
            email.status = "error"
            email.error_detail = _truncate(f"{type(exc).__name__}: {exc}")
            result = "errored"

        if result == "classified":
            classified += 1
        elif result == "extracted":
            extracted += 1
        elif result == "normalized":
            normalized += 1
        elif result == "errored":
            # status / error_detail already set (here or in _extract); just count.
            errored += 1

        session.commit()

    return AdvanceStats(
        classified=classified, extracted=extracted, normalized=normalized, errored=errored
    )


def _step(
    session: Session,
    email: Email,
    blob_store: BlobStore,
    vision_extractor: VisionExtractor,
    llm_client: LLMClient,
) -> str:
    if email.status == "new":
        return _classify(session, email, blob_store)
    if email.status == "classified":
        return _extract(session, email, blob_store, vision_extractor)
    return _normalize(session, email, llm_client)


def _normalize(session: Session, email: Email, llm_client: LLMClient) -> str:
    normalize_one(session, email, llm_client)
    return "normalized"


def _no_content(p: MessagePart) -> bool:
    return (p.is_internal and not p.has_payment_signal) or not p.body_text.strip()


def _split_or_fallback(email: Email) -> list[MessagePart]:
    """Split the body into messages; any splitter failure means one whole-body message."""
    try:
        return split_email(
            body_html=email.body_html,
            body_text=email.body_text,
            sender=email.sender_address,
            received_at=email.received_at,
            client_domains=get_settings().client_domain_list(),
        )
    except Exception:  # noqa: BLE001 -- splitting must never error an email
        logger.exception("thread split failed for email %s; using the whole body", email.id)
        text = email.body_text or ""
        if not text.strip() and email.body_html:
            text = BeautifulSoup(email.body_html, "lxml").get_text(" ")
        return [
            MessagePart(
                position=0,
                sender=None,
                sent_at=None,
                raw_header="",
                body_text=text,
                tables=[],
                is_internal=False,
                has_payment_signal=False,
                fingerprint=None,
            )
        ]


def _ensure_messages(
    session: Session, email: Email, attachments: list[Attachment]
) -> list[EmailMessage]:
    existing = list(
        session.scalars(
            select(EmailMessage)
            .where(EmailMessage.email_id == email.id)
            .order_by(EmailMessage.position)
        )
    )
    if existing:  # reprocess / retry: never re-split, never self-match
        return existing
    parts = _split_or_fallback(email)
    carrier = next((p.position for p in parts if not _no_content(p)), 0) if attachments else None
    rows: list[EmailMessage] = []
    for p in parts:
        carries = carrier is not None and p.position == carrier
        status, reason, seen_in = "new", None, None
        if not carries and _no_content(p):
            status = "no_content"
        elif not carries and p.has_payment_signal:
            match = (
                find_seen_by_fingerprint(session, email_id=email.id, fingerprint=p.fingerprint)
                if p.fingerprint
                else None
            )
            if match is not None:
                status, reason, seen_in = "seen", "fingerprint", match.id
            elif all_references_recorded(session, find_references(p.body_text)):
                status, reason = "seen", "references_recorded"
        row = EmailMessage(
            email_id=email.id,
            position=p.position,
            sender=p.sender,
            sent_at=p.sent_at,
            raw_header=p.raw_header,
            is_internal=p.is_internal,
            carries_attachments=carries,
            body_text=p.body_text if status != "seen" else None,
            tables=p.tables if status != "seen" else None,
            fingerprint=p.fingerprint,
            status=status,
            seen_reason=reason,
            seen_in_message_id=seen_in,
        )
        session.add(row)
        rows.append(row)
    if not rows:  # empty body: one row so attachments still have a home
        row = EmailMessage(
            email_id=email.id,
            position=0,
            sender=email.sender_address.lower(),
            sent_at=email.received_at,
            raw_header="",
            is_internal=False,
            carries_attachments=bool(attachments),
            body_text="",
            tables=[],
            status="new",
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def _classify(session: Session, email: Email, blob_store: BlobStore) -> str:
    atts = list(session.scalars(select(Attachment).where(Attachment.email_id == email.id)))
    messages = _ensure_messages(session, email, atts)
    carrier = next((m for m in messages if m.carries_attachments), None)
    specs: list[tuple[SourceSpec, uuid.UUID | None]] = []
    for spec in classify_email(email, atts, blob_store):
        if spec.ref != "body":  # attachments; the email-level body spec is replaced below
            specs.append((spec, carrier.id if carrier else None))
    live_attachment = any(not s.skipped for s, _ in specs)
    for m in messages:
        if m.status != "new":
            continue
        tables = m.tables or []
        text = m.body_text or ""
        if has_numeric_table_rows(tables):
            specs.append((SourceSpec("body_table", "body"), m.id))
        elif (
            not (m.carries_attachments and live_attachment)
            and len(re.sub(r"\s+", "", text)) >= BODY_TEXT_MIN_CHARS
            and any(ch.isdigit() for ch in text)
        ):
            specs.append((SourceSpec("body_text", "body"), m.id))
    if not any(not s.skipped for s, _ in specs):
        if all(m.status in ("seen", "no_content") for m in messages) or any(
            m.status == "seen" for m in messages
        ):
            email.status = "done"  # everything here is already recorded or empty
            session.flush()
            return "classified"
        specs.append(
            (
                SourceSpec("body_text", "body", skipped=True, skip_reason="no extractable content"),
                None,
            )
        )
    for spec, message_id in specs:
        session.add(
            ExtractionSource(
                email_id=email.id,
                email_message_id=message_id,
                kind=spec.kind,
                ref=spec.ref,
                skipped=spec.skipped,
                skip_reason=spec.skip_reason,
            )
        )
    email.status = "classified"
    session.flush()
    return "classified"


def _extract(
    session: Session,
    email: Email,
    blob_store: BlobStore,
    vision_extractor: VisionExtractor,
) -> str:
    all_sources = list(
        session.scalars(select(ExtractionSource).where(ExtractionSource.email_id == email.id))
    )
    sources = [s for s in all_sources if not s.skipped]
    if not sources:
        # every classified source was skipped -> nothing to extract (I4).
        email.status = "error"
        email.error_detail = "no extractable content"
        return "errored"

    pending = [s for s in sources if not _has_raw_extraction(session, s.id)]
    failures: list[str] = []
    for src in pending:
        try:
            with session.begin_nested():
                content = _run_extractor(session, email, src, blob_store, vision_extractor)
                session.add(
                    RawExtraction(
                        extraction_source_id=src.id,
                        payload=content.to_payload(),
                        extractor_version=EXTRACTOR_VERSION,
                    )
                )
                session.flush()
        except Exception as exc:  # noqa: BLE001 -- per-source isolation is the point
            failures.append(f"{src.kind} {src.id}: {type(exc).__name__}: {exc}")

    if failures:
        email.status = "error"
        email.error_detail = _truncate("; ".join(failures))
        return "errored"

    if all(_has_raw_extraction(session, s.id) for s in sources):
        email.status = "extracted"
        session.flush()
        return "extracted"
    # unreachable: every pending source is handled or recorded as a failure above.
    return "classified"


def _truncate(s: str, n: int = 2000) -> str:
    """Cap an ``error_detail`` string -- openpyxl / pdfplumber messages can embed
    file paths and document content, and Plan 5 renders it verbatim."""
    return s if len(s) <= n else s[: n - 1] + "…"


def _has_raw_extraction(session: Session, source_id: uuid.UUID) -> bool:
    return (
        session.scalar(
            select(RawExtraction.id).where(RawExtraction.extraction_source_id == source_id)
        )
        is not None
    )


def _run_extractor(
    session: Session,
    email: Email,
    src: ExtractionSource,
    blob_store: BlobStore,
    vision_extractor: VisionExtractor,
) -> ExtractedContent:
    if src.ref == "body" and src.email_message_id is not None:
        msg = session.get(EmailMessage, src.email_message_id)
        if msg is None:
            raise ValueError(f"extraction_source {src.id} references a missing message")
        tables = (msg.tables or []) if src.kind == "body_table" else []
        return ExtractedContent(
            text=msg.body_text or "", tables=tables, meta={"email_message_id": str(msg.id)}
        )
    if src.ref == "body":
        if src.kind == "body_table":
            return extract_html_tables(email.body_html)
        if src.kind == "body_text":
            return extract_body_text(email.body_text, email.body_html)
        raise ValueError(f"unsupported body source kind: {src.kind!r}")

    att = session.get(Attachment, uuid.UUID(src.ref))
    if att is None:
        raise ValueError(f"extraction_source {src.id} references missing attachment {src.ref}")
    data = blob_store.get(attachment_blob_key(att))

    if src.kind == "excel":
        return extract_excel(data)
    if src.kind == "pdf_text":
        return extract_pdf(data)
    if src.kind == "pdf_scanned":
        # classification already confirmed a PDF (a scanned one may arrive as
        # application/octet-stream), so name the media type explicitly.
        return vision_extractor.extract_image(data, "application/pdf")
    if src.kind == "image":
        return vision_extractor.extract_image(data, att.content_type)
    raise ValueError(f"unsupported attachment source kind: {src.kind!r}")
