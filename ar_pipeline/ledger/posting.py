"""Write approved payments into the ledger.

Called from ``pipeline.routing.approve_and_queue`` — the single approval path
shared by auto-approve and human review — so every approval posts exactly once.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Extraction, Invoice, InvoicePayment
from ar_pipeline.ledger.matching import number_key
from ar_pipeline.schema.canonical import RemittancePayload

log = logging.getLogger(__name__)


def find_invoice(session: Session, invoice_number: str) -> Invoice | None:
    key = number_key(invoice_number)
    if not key:
        return None
    return session.scalar(select(Invoice).where(Invoice.number_key == key))


def _get_or_create_email_invoice(
    session: Session,
    *,
    invoice_number: str,
    amount: Decimal,
    payer_name: str | None,
    currency: str,
    invoice_date: object,
) -> Invoice:
    key = number_key(invoice_number)
    # insert-or-fetch: two approvals creating the same unverified invoice at once
    # must not collide on uq_invoice_number_key.
    session.execute(
        pg_insert(Invoice)
        .values(
            id=uuid.uuid4(),
            invoice_number=invoice_number.strip(),
            number_key=key,
            payer_name=payer_name or None,
            invoice_date=invoice_date,
            amount=amount,
            currency=currency,
            source="email",
            paid_before_import=Decimal("0"),
        )
        .on_conflict_do_nothing(index_elements=["number_key"])
    )
    invoice = session.scalar(select(Invoice).where(Invoice.number_key == key))
    assert invoice is not None
    return invoice


def post_extraction(session: Session, extraction: Extraction) -> int:
    canonical = extraction.canonical
    if not extraction.is_remittance or not isinstance(canonical, dict) or not canonical:
        return 0
    try:
        payload = RemittancePayload.model_validate(canonical)
    except ValidationError:
        log.warning("extraction %s: canonical does not validate; not posted", extraction.id)
        return 0
    header = payload.header
    posted = 0
    for i, line in enumerate(payload.line_items):
        if not number_key(line.invoice_number):
            continue
        already = session.scalar(
            select(InvoicePayment.id).where(
                InvoicePayment.extraction_id == extraction.id, InvoicePayment.line_index == i
            )
        )
        if already is not None:
            continue
        deductions = sum((d.amount for d in line.deductions), Decimal("0"))
        settled = line.amount_paid + deductions
        invoice = find_invoice(session, line.invoice_number) or _get_or_create_email_invoice(
            session,
            invoice_number=line.invoice_number,
            amount=line.invoice_amount if line.invoice_amount > 0 else settled,
            payer_name=header.payer_name,
            currency=header.currency,
            invoice_date=line.invoice_date,
        )
        session.add(
            InvoicePayment(
                invoice_id=invoice.id,
                extraction_id=extraction.id,
                line_index=i,
                amount_paid=line.amount_paid,
                deductions_total=deductions,
                settled=settled,
                currency=header.currency,
                payment_reference=header.payment_reference,
                payment_date=header.payment_date,
            )
        )
        posted += 1
    session.flush()
    return posted


def backfill(session: Session) -> tuple[int, int]:
    """Post every approved or already-recorded extraction. Safe to re-run."""
    approved = session.scalars(
        select(Extraction)
        .where(Extraction.status.in_(("approved", "already_recorded")))
        .order_by(Extraction.reviewed_at.asc().nulls_last(), Extraction.id.asc())
    )
    extractions = rows = 0
    for ext in approved:
        n = post_extraction(session, ext)
        if n:
            extractions += 1
            rows += n
    return extractions, rows
