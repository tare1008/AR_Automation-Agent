"""Checks a payment against the invoice ledger (spec §2).

Runs where a DB session exists — ``normalize.service.normalize_one`` and
``review.service.save_edits`` — after the pure ``validate_payload``. Every flag
starts with ``line {i}: `` so the review screen pins it to that line.
"""

from __future__ import annotations

import re
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Extraction, Invoice, InvoicePayment
from ar_pipeline.ledger.balance import TOLERANCE, balance_for, status_for
from ar_pipeline.ledger.matching import near_matches, number_key, payers_differ
from ar_pipeline.ledger.money import format_money
from ar_pipeline.normalize.validators import validate_payload
from ar_pipeline.schema.canonical import RemittancePayload

# "draft N: schema validation failed: …" flags come from the normalizer about
# drafts that never became a payload, so re-validating the payload can't
# reproduce them — keep them.
_DRAFT_FLAG_RE = re.compile(r"^draft\s+\d+:")


def _is_duplicate(session: Session, invoice: Invoice, reference: str, amount_paid: Decimal) -> bool:
    return (
        session.scalar(
            select(func.count())
            .select_from(InvoicePayment)
            .where(
                InvoicePayment.invoice_id == invoice.id,
                InvoicePayment.payment_reference == reference,
                InvoicePayment.amount_paid == amount_paid,
            )
        )
        or 0
    ) > 0


def check_against_ledger(session: Session, payload: RemittancePayload) -> list[str]:
    header = payload.header
    books_loaded = (
        session.scalar(select(func.count()).select_from(Invoice).where(Invoice.source == "books"))
        or 0
    ) > 0
    every_invoice: list[Invoice] | None = None
    flags: list[str] = []
    # Σ settled by earlier lines of this payment, per number key (two lines for
    # "INV-1" and "INV 1" must not each pass against the full outstanding).
    earlier: dict[str, Decimal] = {}

    for i, line in enumerate(payload.line_items):
        if not number_key(line.invoice_number):
            continue  # "empty invoice number" is already flagged by validate_payload
        label = f"line {i}: {line.invoice_number}"
        settled = line.amount_paid + sum((d.amount for d in line.deductions), Decimal("0"))
        # the line only claims an invoice total when that total differs from what it
        # settles; an equal pair just describes the payment (spec §2 "Stated vs settled").
        states_total = abs(line.invoice_amount - settled) > TOLERANCE
        key = number_key(line.invoice_number)
        # serialize check-then-post per invoice across concurrent approvals: row
        # lock on the invoice, or an advisory lock on the key while it doesn't
        # exist yet. Both release at commit/rollback (spec §2 "Re-check at approval").
        invoice = session.scalar(select(Invoice).where(Invoice.number_key == key).with_for_update())

        if invoice is None:
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
            if every_invoice is None:
                every_invoice = list(session.scalars(select(Invoice)))
            near = near_matches(line.invoice_number, every_invoice, header.payer_name)
            if near:
                names = ", ".join(inv.invoice_number for inv in near)
                flags.append(f"line {i}: {line.invoice_number} not found — did you mean {names}?")
            elif books_loaded:
                flags.append(f"{label} isn't in your open invoices")
            if states_total and settled < line.invoice_amount:
                flags.append(
                    f"{label} — partial payment {format_money(settled, header.currency)} of "
                    f"{format_money(line.invoice_amount, header.currency)}; invoice amount comes "
                    "from this email, not your books"
                )
            continue

        def money(value: Decimal, _cur: str = invoice.currency) -> str:
            return format_money(value, _cur)

        if header.currency != invoice.currency:
            flags.append(
                f"{label} — payment in {header.currency}, invoice in {invoice.currency}; "
                "not applied to the balance"
            )
            continue
        if payers_differ(header.payer_name, invoice.payer_name):
            flags.append(
                f"{label} — invoice belongs to {invoice.payer_name}; "
                f"payment is from {header.payer_name}"
            )
        if states_total and abs(line.invoice_amount - invoice.amount) > TOLERANCE:
            where = "your books say" if invoice.source == "books" else "an earlier email said"
            flags.append(
                f"{label} — email says invoice {money(line.invoice_amount)}, "
                f"{where} {money(invoice.amount)}"
            )
        if header.payment_reference and _is_duplicate(
            session, invoice, header.payment_reference, line.amount_paid
        ):
            flags.append(
                f"{label} — reference {header.payment_reference} for "
                f"{money(line.amount_paid)} was already approved"
            )
        bal = balance_for(session, invoice)
        before = earlier.get(key, Decimal("0"))
        earlier[key] = before + settled
        outstanding = bal.outstanding - before
        if status_for(invoice.amount, bal.paid + before) in ("paid", "overpaid"):
            flags.append(f"{label} is already fully paid")
        elif settled > outstanding + TOLERANCE:
            flags.append(
                f"{label} — pays {money(settled)} but only {money(outstanding)} "
                f"outstanding; overpaid by {money(settled - outstanding)}"
            )
        elif settled < outstanding - TOLERANCE and invoice.source == "email":
            flags.append(
                f"{label} — partial payment {money(settled)} of {money(outstanding)} "
                "outstanding; invoice amount comes from an email, not your books"
            )
    return flags


def refresh_pending_flags(session: Session) -> int:
    """Recompute the flags of every pending remittance against the current
    ledger (after a CSV import or a backfill). Returns how many rows changed."""
    changed = 0
    pending = session.scalars(
        select(Extraction).where(
            Extraction.status == "pending_review", Extraction.is_remittance.is_(True)
        )
    )
    for ext in pending:
        try:
            payload = RemittancePayload.model_validate(ext.canonical or {})
        except ValidationError:
            continue
        old = list(ext.validation_flags or [])
        kept = [f for f in old if _DRAFT_FLAG_RE.match(f)]
        new = validate_payload(payload) + check_against_ledger(session, payload) + kept
        if new != old:
            ext.validation_flags = new
            changed += 1
    session.flush()
    return changed
