"""Deterministic post-extraction checks on a canonical ``RemittancePayload``.

``validate_payload`` is a pure function: no DB, no I/O, no import-time side
effects. It returns a list of human-readable flag strings; an empty list means
the payload passed every check. Bump ``CHECK_VERSION`` whenever the checks or
their wording change.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from ar_pipeline.schema.canonical import RemittancePayload

CHECK_VERSION = "1"

_TOLERANCE = Decimal("0.02")
_MAX_BACKDATE = timedelta(days=400)
_FUTURE_GRACE = timedelta(days=2)


def validate_payload(payload: RemittancePayload) -> list[str]:
    header = payload.header
    line_items = payload.line_items
    flags: list[str] = []

    # 1. line net identity
    for i, line in enumerate(line_items):
        sum_ded = sum((d.amount for d in line.deductions), Decimal("0"))
        residual = line.invoice_amount - sum_ded - line.amount_paid
        if abs(residual) > _TOLERANCE:
            flags.append(
                f"line {i} ({line.invoice_number}): invoice {line.invoice_amount} "
                f"- deductions {sum_ded} != amount_paid {line.amount_paid}"
            )

    # 2. payment total identity
    sum_lines = sum((li.amount_paid for li in line_items), Decimal("0"))
    sum_hdr = sum((d.amount for d in header.deductions), Decimal("0"))
    expected = sum_lines - sum_hdr
    if abs(expected - header.total_paid_amount) > _TOLERANCE:
        flags.append(
            f"payment total {header.total_paid_amount} != sum(line amount_paid) "
            f"{sum_lines} - header deductions {sum_hdr} = {expected}"
        )

    # 3. negative amounts (deductions are already ge=0 by schema)
    for i, line in enumerate(line_items):
        if line.invoice_amount < 0:
            flags.append(f"line {i}: negative invoice_amount {line.invoice_amount}")
        if line.amount_paid < 0:
            flags.append(f"line {i}: negative amount_paid {line.amount_paid}")
    if header.total_paid_amount < 0:
        flags.append(f"negative total_paid_amount {header.total_paid_amount}")

    # 4. duplicate invoice numbers (once per duplicated value, first-seen order)
    counts = Counter(li.invoice_number for li in line_items if li.invoice_number.strip() != "")
    for number, count in counts.items():
        if count > 1:
            flags.append(f"duplicate invoice number: {number}")

    # 5. empty invoice number
    for i, line in enumerate(line_items):
        if line.invoice_number.strip() == "":
            flags.append(f"line {i}: empty invoice number")

    # 6. payment_date sanity
    payment_date = header.payment_date
    if payment_date is not None:
        for i, line in enumerate(line_items):
            invoice_date = line.invoice_date
            if invoice_date is not None and invoice_date - payment_date > _MAX_BACKDATE:
                flags.append(
                    f"line {i}: payment_date {payment_date} is more than 400 days "
                    f"before invoice_date {invoice_date}"
                )
        cutoff = date.today() + _FUTURE_GRACE
        if payment_date > cutoff:
            flags.append(f"payment_date {payment_date} is after {cutoff}")

    # 7. currency (informational; the pipeline is INR-only for now)
    if header.currency != "INR":
        flags.append(f"non-INR currency: {header.currency}")

    return flags
