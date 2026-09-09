"""Advance emails one pipeline state at a time: ``new -> classified -> extracted``.

``advance_once`` pulls a batch of pending emails and moves each forward exactly
one state. Every email is processed inside its own ``session.begin_nested()``
savepoint so a failing step poisons only that email. Extraction goes further:
each source is extracted inside its *own* nested savepoint, so a corrupt
attachment fails only its own source and its healthy siblings' ``RawExtraction``
rows survive (a manual retry then re-runs only the failed source). The caller
owns the outer transaction -- ``advance_once`` never commits.
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
from ar_pipeline.storage import BlobStore

_PENDING_STATUSES = ("new", "classified")


@dataclass(frozen=True)
class AdvanceStats:
    classified: int = 0
    extracted: int = 0
    errored: int = 0


def advance_once(
    session: Session,
    blob_store: BlobStore,
    vision_extractor: VisionExtractor,
    *,
    batch: int = 20,
) -> AdvanceStats:
    emails = list(
        session.scalars(
            select(Email)
            .where(Email.status.in_(_PENDING_STATUSES))
            .order_by(Email.received_at.asc())
            .limit(batch)
        )
    )

    classified = extracted = errored = 0
    for email in emails:
        try:
            with session.begin_nested():
                result = _step(session, email, blob_store, vision_extractor)
        except Exception as exc:  # savepoint already rolled back
            email.status = "error"
            email.error_detail = _truncate(f"{type(exc).__name__}: {exc}")
            errored += 1
            continue

        if result == "classified":
            classified += 1
        elif result == "extracted":
            extracted += 1
        elif result == "errored":
            # _extract already set status/error_detail; just count it.
            errored += 1

    return AdvanceStats(classified=classified, extracted=extracted, errored=errored)


def _step(
    session: Session,
    email: Email,
    blob_store: BlobStore,
    vision_extractor: VisionExtractor,
) -> str:
    if email.status == "new":
        return _classify(session, email, blob_store)
    return _extract(session, email, blob_store, vision_extractor)


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
    data = blob_store.get(f"{att.email_id}/{att.id}/{att.filename}")

    if src.kind == "excel":
        return extract_excel(data)
    if src.kind == "pdf_text":
        return extract_pdf(data)
    if src.kind in ("pdf_scanned", "image"):
        return vision_extractor.extract_image(data, att.content_type)
    raise ValueError(f"unsupported attachment source kind: {src.kind!r}")
