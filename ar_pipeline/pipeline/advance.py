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

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ar_pipeline.classify.classifier import classify_email
from ar_pipeline.db.models import Attachment, Email, ExtractionSource, RawExtraction
from ar_pipeline.extract.base import EXTRACTOR_VERSION, ExtractedContent
from ar_pipeline.extract.body_text import extract_body_text
from ar_pipeline.extract.excel import extract_excel
from ar_pipeline.extract.html_table import extract_html_tables
from ar_pipeline.extract.pdf import extract_pdf
from ar_pipeline.extract.vision import VisionExtractor
from ar_pipeline.normalize.llm_client import LLMClient
from ar_pipeline.normalize.service import normalize_one
from ar_pipeline.storage import BlobStore, attachment_blob_key

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


def _classify(session: Session, email: Email, blob_store: BlobStore) -> str:
    atts = list(session.scalars(select(Attachment).where(Attachment.email_id == email.id)))
    for spec in classify_email(email, atts, blob_store):
        session.add(
            ExtractionSource(
                email_id=email.id,
                kind=spec.kind,
                ref=spec.ref,
                skipped=spec.skipped,
                skip_reason=spec.skip_reason,
            )
        )
    session.flush()
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
