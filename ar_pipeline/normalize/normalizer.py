"""Turn one email's raw extractions into canonical ``RemittancePayload``s.

``normalize_email`` builds the user message, asks the ``LLMClient`` for a
structured ``NormalizerOutput`` (one ``PaymentDraft`` per distinct payment),
fills the ``Envelope`` we own, runs the deterministic validators, and returns
the wrapper plus one ``NormalizedPayment`` per payment that survived schema
construction.

Pure module — no DB, no network, no import-time side effects. ``LLMRefused`` /
``LLMError`` from the client propagate to the caller.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ar_pipeline.normalize.llm_client import LLMClient
from ar_pipeline.normalize.prompt import SYSTEM_PROMPT, build_user_message
from ar_pipeline.normalize.validators import validate_payload
from ar_pipeline.schema.canonical import (
    Deduction,
    Envelope,
    Header,
    LineItem,
    RemittancePayload,
)

log = logging.getLogger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")


class PaymentDraft(BaseModel):
    """The LLM's per-payment output — everything except the envelope we own."""

    model_config = ConfigDict(extra="forbid")

    payer_name: str
    payer_id: str | None = None
    payment_reference: str | None = None
    payment_reference_type: str | None = None
    payment_date: date | None = None
    payment_method: str | None = None
    currency: str = "INR"
    total_paid_amount: Decimal
    header_deductions: list[Deduction] = Field(default_factory=list)
    line_items: list[LineItem] = Field(default_factory=list)
    vendor_guess: str | None = None
    confidence: float = 0.0

    @field_validator("currency", mode="before")
    @classmethod
    def _currency_fallback(cls, v: object) -> str:
        # Losing a whole payment over a malformed currency ("Rupees") is wrong;
        # the canonical Header only accepts a 3-letter code, so fall back to INR
        # and let validator check 7 flag any genuine non-INR payment instead.
        if isinstance(v, str) and re.fullmatch(r"[A-Za-z]{3}", v):
            return v
        return "INR"


class NormalizerOutput(BaseModel):
    """The full structured response for one email."""

    model_config = ConfigDict(extra="forbid")

    is_remittance: bool
    notes: str = ""
    payments: list[PaymentDraft] = Field(default_factory=list)


@dataclass(frozen=True)
class NormalizedPayment:
    payload: RemittancePayload
    confidence: Decimal
    is_remittance: bool
    validation_flags: list[str]
    notes: str
    raw_llm_response: dict


def normalize_email(
    *,
    email_id: str,
    sender_address: str,
    subject: str,
    raw_extractions: list[dict],
    llm_client: LLMClient,
) -> tuple[NormalizerOutput, list[NormalizedPayment]]:
    user = build_user_message(sender_address, subject, raw_extractions)
    out = llm_client.parse(system=SYSTEM_PROMPT, user=user, output_model=NormalizerOutput)
    raw = out.model_dump(mode="json")

    results: list[NormalizedPayment] = []
    skipped: list[str] = []
    next_index = 0

    for i, draft in enumerate(out.payments):
        try:
            hdr = Header(
                payer_name=draft.payer_name,
                payer_id=draft.payer_id,
                payment_reference=draft.payment_reference,
                payment_reference_type=draft.payment_reference_type,
                payment_date=draft.payment_date,
                payment_method=draft.payment_method,
                currency=draft.currency or "INR",
                total_paid_amount=draft.total_paid_amount,
                deductions=draft.header_deductions,
            )
            env = Envelope(
                extraction_id=str(uuid.uuid4()),
                source_email_id=email_id,
                payment_index=next_index,
                vendor_guess=draft.vendor_guess,
                extracted_at=datetime.now(UTC),
                reviewed_by=None,
            )
            payload = RemittancePayload(envelope=env, header=hdr, line_items=list(draft.line_items))
        except ValidationError as exc:
            skipped.append(f"draft {i}: schema validation failed: {exc}")
            continue

        flags = validate_payload(payload)

        conf = Decimal(str(draft.confidence))
        # a NaN/Inf confidence would raise InvalidOperation in the clamp below
        if not conf.is_finite() or conf < _ZERO:
            conf = _ZERO
        elif conf > _ONE:
            conf = _ONE
        conf = conf.quantize(Decimal("0.001"))

        # Payment 0 carries the full LLM dump; the rest store just their own
        # draft plus a pointer, so a 12-payment email is not 12 JSONB copies.
        if results:
            payment_raw: dict = {
                "payment_draft": draft.model_dump(mode="json"),
                "see": "payment 0 raw_llm_response for the full LLM output",
            }
        else:
            payment_raw = raw

        results.append(
            NormalizedPayment(
                payload=payload,
                confidence=conf,
                is_remittance=out.is_remittance,
                validation_flags=flags,
                notes=out.notes,
                raw_llm_response=payment_raw,
            )
        )
        next_index += 1

    if skipped:
        if results:
            results[0].validation_flags.extend(skipped)
        else:
            out.notes = (out.notes + " | " + "; ".join(skipped)).strip(" |")

    return out, results
