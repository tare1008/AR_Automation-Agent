"""Persist one email's normalized payments as ``extraction`` rows.

``normalize_one`` gathers an email's ``raw_extraction`` payloads, runs
``normalize_email`` (LLM + deterministic validators), writes one ``Extraction``
row per payment — or a single ``is_remittance=false`` row when the LLM says the
material is not a remittance / has nothing to extract — and flips the email to
``review``.

It does NOT commit: the caller (``advance_once``) owns the transaction boundary
and commits per email. Pure at import — no DB, no network.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ar_pipeline.config import get_settings
from ar_pipeline.db.models import Email, Extraction, ExtractionSource, RawExtraction
from ar_pipeline.normalize.llm_client import LLMClient
from ar_pipeline.normalize.normalizer import normalize_email
from ar_pipeline.normalize.prompt import PROMPT_VERSION

__all__ = ["normalize_one"]


def normalize_one(session: Session, email: Email, llm_client: LLMClient) -> int:
    """Normalize one ``status="extracted"`` email into ``extraction`` rows.

    Returns the number of ``Extraction`` rows added. Sets ``email.status`` to
    ``"review"`` and flushes; does not commit.
    """
    raw_extractions = list(
        session.scalars(
            select(RawExtraction.payload)
            .join(ExtractionSource, RawExtraction.extraction_source_id == ExtractionSource.id)
            .where(ExtractionSource.email_id == email.id)
            # deterministic source order: ExtractionSource.id is a uuid4 PK, so
            # anything keyed on it reshuffles the prompt every run (I1).
            .order_by(ExtractionSource.kind, ExtractionSource.ref, RawExtraction.id)
        )
    )

    if not raw_extractions:
        email.status = "error"
        email.error_detail = "no raw extractions to normalize"
        session.flush()
        return 0

    model = get_settings().llm_model

    out, payments = normalize_email(
        email_id=str(email.id),
        sender_address=email.sender_address,
        subject=email.subject,
        raw_extractions=raw_extractions,
        llm_client=llm_client,
    )

    rows: list[Extraction] = []
    if payments:
        for payment in payments:
            row = Extraction(
                email_id=email.id,
                canonical=payment.payload.model_dump(mode="json"),
                confidence=payment.confidence,
                is_remittance=payment.is_remittance,
                validation_flags=list(payment.validation_flags),
                llm_model=model,
                prompt_version=PROMPT_VERSION,
                raw_llm_response=dict(payment.raw_llm_response),
                status="pending_review",
            )
            session.add(row)
            rows.append(row)
        added = len(payments)
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
        added = 1

    email.status = "review"
    session.flush()
    # the normalizer minted a placeholder uuid for envelope.extraction_id
    # before these rows had a PK; make it authoritative now.
    for row in rows:
        env = row.canonical.get("envelope")
        if isinstance(env, dict):
            # top-level reassignment so MutableDict tracks the change
            row.canonical["envelope"] = {**env, "extraction_id": str(row.id)}
    if rows:
        session.flush()
    return added
