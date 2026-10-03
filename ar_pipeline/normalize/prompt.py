"""The normalizer's LLM prompt: a versioned system instruction plus a renderer
that folds one email's raw extractions into a single user message.

Pure module — no DB, no network, no import-time side effects. Bump
``PROMPT_VERSION`` whenever ``SYSTEM_PROMPT`` or ``build_user_message`` changes
in a way that could move the model's output; it is stored on every
``extraction`` row.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ar_pipeline.tables.mapping import LineTable

log = logging.getLogger(__name__)

PROMPT_VERSION = "5"

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
debit_note, advance_adjustment, discount, rounding, other. `amount` is ALWAYS a positive \
number: the amount withheld or subtracted, never negative, never a signed \
delta. Indian buyers commonly withhold TDS under section 194Q at roughly 0.1% \
of the invoice, and TDS, credit notes and advance adjustments frequently stack \
on the same invoice. Put a deduction on the line item it applies to; put a \
deduction that applies to the whole payment (not a specific invoice) in \
`header_deductions`.

Adjustments. Some advices list rows that are not invoices but reduce an \
earlier invoice — discounts, debit notes, credit notes, often numbered after \
the invoice they reduce (e.g. "2510004583DISCO"). Emit each as a line item with \
kind="adjustment": invoice_number = the adjustment's own number, \
invoice_amount = 0, one deduction {type: discount | debit_note | credit_note, \
amount: X}, amount_paid = -X, and applies_to = the invoice it reduces when the \
advice names it, else null. Every ordinary invoice line has kind="invoice".

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

`currency` is always a three-letter ISO 4217 code, never a symbol or a word: \
write "$" or "US$" as USD, "€" as EUR, "£" as GBP, "₹" / "Rs" / "Rupees" as \
INR. It defaults to INR when the material does not say otherwise. \
`payment_reference` is the bank UTR / RTGS / NEFT reference if one is present, \
otherwise null; `payment_reference_type` is one of "utr", "rtgs", "neft", \
"request_number", "cheque", "payer_document", or null. When the advice has no \
bank reference but shows the payer's own document number (e.g. "Document No : \
1500005408"), put that number in `payment_reference` with type "payer_document". \
Many advices carry no bank reference at all — that is fine, leave both null.

`vendor_guess` is the vendor / remitter's name as best you can tell from the \
content. `payer_id` is the payer's customer / vendor code if the advice shows \
one. `payment_method` is "RTGS" / "NEFT" / "cheque" / "net banking" etc. if \
stated. `invoice_date` is the invoice's own date, not the payment date.

`confidence` (0 to 1) is your calibrated confidence that this payment has been \
transcribed correctly and completely. `notes` is free text for anything a human \
reviewer should know (ambiguities, assumptions, multiple attachments that \
disagree).
"""

MAPPING_SYSTEM_PROMPT = """\
You map the columns of one payment-advice table to line-item fields. The table \
lists the invoices that one payment settles, possibly with adjustment rows \
(discounts, debit notes). Give each column index one role: invoice_number (the \
row's bill / invoice / document number), invoice_date, invoice_amount (gross \
invoice value), tds (tax deducted at source), adjustment (advance / debit / \
discount column), other_deduction, amount_paid (net amount paid for the row), \
payment_reference (a bank UTR / RTGS / NEFT number per row), or ignore. Use each \
role at most once; invoice_number and amount_paid are required. If the table is \
not a list of invoices settled by one payment — for example every row is a \
separate bank transfer, or it is not a payment table — set is_line_table=false.
"""

HEADER_SYSTEM_PROMPT = """\
You read the header of one payment advice: who paid, when, how, and the \
payment's reference. Its invoice table has already been read separately; only \
its first rows are shown. is_remittance is false if this is not a payment \
advice or remittance at all. payer_name is the company that made the payment — \
usually the letterhead, not the "Vendor Name" or beneficiary, which is the \
company receiving the money. payer_id is the payer's vendor / customer code if \
shown. payment_reference is the bank UTR / RTGS / NEFT reference with \
payment_reference_type "utr" / "rtgs" / "neft"; when there is no bank \
reference but the payer's own document number is shown (e.g. "Document No : \
1500005408"), put that number with type "payer_document"; otherwise null. \
payment_date is the payment or document date. currency is a three-letter ISO \
code (INR unless stated). confidence (0 to 1) is how sure you are of these \
fields; notes is anything a reviewer should know. Do not invent values: leave \
anything not shown as null.
"""


def system_prompt_for(client_names: list[str], base: str = SYSTEM_PROMPT) -> str:
    """``base`` plus who the receiving company is. Constant per deployment, so
    the cached prefix stays identical across calls."""
    if not client_names:
        return base
    return base + (
        f"\nThe receiving company — the payee, never the payer — is "
        f'{", ".join(client_names)}. An advice may print it as "Vendor Name" or '
        f'"Beneficiary"; the payer is the other party.\n'
    )


def build_mapping_message(table: LineTable) -> str:
    lines = ["Columns:"]
    lines += [f"{i} = {cell}" for i, cell in enumerate(table.header)]
    lines += ["", "First rows:"]
    lines += [" | ".join(row) for row in table.rows[:3]]
    return "\n".join(lines)


def build_header_message(
    sender_address: str, subject: str, raw_extractions: list[dict], table: LineTable
) -> str:
    trimmed: list[dict] = []
    for si, raw in enumerate(raw_extractions):
        tables = list(raw.get("tables") or [])
        if si == table.source_index and table.table_index < len(tables):
            shown: list[list[str]] = [table.header, *table.rows[:3]]
            hidden = len(table.rows) - 3
            if hidden > 0:
                shown.append([f"[... {hidden} more rows read separately ...]"])
            if table.total_row is not None:
                shown.append(table.total_row)
            tables[table.table_index] = shown
        trimmed.append({**raw, "tables": tables})
    return build_user_message(sender_address, subject, trimmed)


def _render(sender_address: str, subject: str, raw_extractions: list[dict]) -> str:
    """Everything ``build_user_message`` renders, before the length cap."""
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

    return "\n".join(parts)


def is_truncated(sender_address: str, subject: str, raw_extractions: list[dict]) -> bool:
    """True when ``build_user_message`` would cut this material at the length cap."""
    return len(_render(sender_address, subject, raw_extractions)) > _MAX_USER_CHARS


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
    rendered = _render(sender_address, subject, raw_extractions)

    if len(rendered) > _MAX_USER_CHARS:
        log.warning("normalize: user message truncated for %s", subject)
        rendered = rendered[:_MAX_USER_CHARS] + _TRUNCATION_MARKER

    return rendered
