"""The normalizer's LLM prompt: a versioned system instruction plus a renderer
that folds one email's raw extractions into a single user message.

Pure module — no DB, no network, no import-time side effects. Bump
``PROMPT_VERSION`` whenever ``SYSTEM_PROMPT`` or ``build_user_message`` changes
in a way that could move the model's output; it is stored on every
``extraction`` row.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

PROMPT_VERSION = "2"

_MAX_USER_CHARS = 40_000
_TRUNCATION_MARKER = "\n\n[... content truncated ...]"

SYSTEM_PROMPT = """\
You extract accounts-receivable settlement (remittance / payment-advice) data \
from the material below into a structured form. The material is whatever a \
company's AR inbox received: the body of an email and the text and tables \
pulled from its attachments.

These emails are very often internal forwards of a customer's payment advice. \
The party that actually made the payment — the remitter, the vendor, the payer \
— is named inside the forwarded content or an attachment, NOT in the address \
that sent the email to us. Read the content to find who paid.

Produce one payload per DISTINCT PAYMENT. A payment is one bank transfer. Two \
rows belong to different payments when they carry a different bank reference \
(UTR / RTGS / NEFT number) OR a different value date. One payment that settles \
many invoices is ONE payment with many line items — do not split it. If the \
material clearly is not a remittance or payment advice at all, set \
is_remittance=false and return payments=[].

Deductions. Each line item and the payment header carry a `deductions` list of \
`{type, amount, reason}`. `type` is one of tds, credit_note, \
advance_adjustment, discount, rounding, other. `amount` is ALWAYS a positive \
number: the amount withheld or subtracted, never negative, never a signed \
delta. Indian buyers commonly withhold TDS under section 194Q at roughly 0.1% \
of the invoice, and TDS, credit notes and advance adjustments frequently stack \
on the same invoice. Put a deduction on the line item it applies to; put a \
deduction that applies to the whole payment (not a specific invoice) in \
`header_deductions`.

Every payment must have at least one line item. If the material gives only a \
payment total with no per-invoice breakdown, emit ONE line item: use the \
invoice or reference number you can find (or an empty invoice number if there \
is none), set `invoice_amount` and `amount_paid` to the payment total.

Two identities must hold. Per line item: `invoice_amount - sum(deductions) = \
amount_paid`. Per payment: `total_paid_amount = sum(line item amount_paid) - \
sum(header_deductions)`.

Do not invent values. Set any field that is not present in the material to \
null. Preserve invoice numbers, UTRs and every amount verbatim: strip currency \
symbols and digit-group separators, keep amounts as plain decimal numbers \
(e.g. "1,23,456.78" becomes 123456.78), but do not round, reformat or \
recompute them.

`currency` defaults to INR when the material does not say otherwise. \
`payment_reference` is the bank UTR / RTGS / NEFT reference if one is present, \
otherwise null; `payment_reference_type` is one of "utr", "rtgs", "neft", \
"request_number", "cheque", or null. Many advices carry no bank reference at \
all — that is fine, leave both null.

`vendor_guess` is the vendor / remitter's name as best you can tell from the \
content. `payer_id` is the payer's customer / vendor code if the advice shows \
one. `payment_method` is "RTGS" / "NEFT" / "cheque" / "net banking" etc. if \
stated. `invoice_date` is the invoice's own date, not the payment date.

`confidence` (0 to 1) is your calibrated confidence that this payment has been \
transcribed correctly and completely. `notes` is free text for anything a human \
reviewer should know (ambiguities, assumptions, multiple attachments that \
disagree).
"""


def build_user_message(
    sender_address: str,
    subject: str,
    raw_extractions: list[dict],
) -> str:
    """Render the sender, subject and every raw extraction into one string.

    Each raw extraction is ``{"text": str, "tables": list[list[list[str]]],
    "meta": dict}``. Tables render one row per line as ``cell | cell | ...``.
    If the rendered message would exceed ~40k chars it is cut to the first 40k
    with a truncation marker appended and a warning logged — never silently
    dropped.
    """
    parts: list[str] = [f"From: {sender_address}", f"Subject: {subject}", ""]

    for n, extraction in enumerate(raw_extractions, start=1):
        meta = extraction.get("meta") or {}
        via = meta.get("via") or "deterministic"
        parts.append(f"--- source {n} ({via}) ---")

        text = extraction.get("text") or ""
        if text:
            parts.append(text)

        tables = extraction.get("tables") or []
        if tables:
            parts.append("Tables:")
            for table in tables:
                for row in table:
                    parts.append(" | ".join(str(cell) for cell in row))
        parts.append("")

    rendered = "\n".join(parts)

    if len(rendered) > _MAX_USER_CHARS:
        log.warning("normalize: user message truncated for %s", subject)
        rendered = rendered[:_MAX_USER_CHARS] + _TRUNCATION_MARKER

    return rendered
