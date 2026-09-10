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
from ar_pipeline.normalize.llm_client import get_llm_client as get_normalize_llm_client
from ar_pipeline.normalize.normalizer import normalize_email
from ar_pipeline.normalize.prompt import PROMPT_VERSION

__all__ = ["get_normalize_llm_client", "normalize_one"]


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
            .order_by(RawExtraction.extraction_source_id)
        )
    )

    model = get_settings().llm_model

    out, payments = normalize_email(
        email_id=str(email.id),
        sender_address=email.sender_address,
        subject=email.subject,
        raw_extractions=raw_extractions,
        llm_client=llm_client,
    )

    if payments:
        for payment in payments:
            session.add(
                Extraction(
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
            )
        added = len(payments)
    else:
        session.add(
            Extraction(
                email_id=email.id,
                canonical={},
                confidence=Decimal("0"),
                is_remittance=out.is_remittance,
                validation_flags=(
                    ["LLM: not a remittance"]
                    if not out.is_remittance
                    else ["LLM returned no payments"]
                ),
                llm_model=model,
                prompt_version=PROMPT_VERSION,
                raw_llm_response=out.model_dump(mode="json"),
                status="pending_review",
            )
        )
        added = 1

    email.status = "review"
    session.flush()
    return added
