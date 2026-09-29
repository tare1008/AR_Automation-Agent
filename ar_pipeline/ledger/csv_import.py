"""Import the client's open invoices from a CSV export (spec §3 "CSV import").

The CSV stands in for the client's ERP: its amounts win over anything learned
from emails. Whole-file problems raise ``CsvImportError``; bad rows are skipped
with a reason naming the spreadsheet row number (header = row 1).
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ar_pipeline.db.models import Invoice, InvoicePayment
from ar_pipeline.ledger.balance import TOLERANCE
from ar_pipeline.ledger.matching import number_key
from ar_pipeline.ledger.money import format_money
from ar_pipeline.ledger.posting import find_invoice

MAX_BYTES = 1_000_000
MAX_ROWS = 5000
REQUIRED = ("invoice_number", "invoice_amount")
COLUMNS = (
    "invoice_number",
    "payer_name",
    "invoice_date",
    "invoice_amount",
    "currency",
    "outstanding_amount",
)
TEMPLATE_CSV = ",".join(COLUMNS) + "\n"
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class CsvImportError(ValueError):
    pass


@dataclass
class ImportResult:
    imported: int = 0
    updated: int = 0
    skipped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Row:
    number: str
    payer_name: str | None
    invoice_date: date | None
    amount: Decimal
    currency: str
    outstanding: Decimal | None


def _money(raw: str) -> Decimal:
    value = Decimal(raw.replace(",", ""))
    if not value.is_finite():
        raise InvalidOperation
    return value


def _parse(n: int, raw: dict[str, str]) -> _Row | str:
    number = raw.get("invoice_number", "")
    if not number:
        return f"row {n}: invoice_number is blank"
    try:
        amount = _money(raw.get("invoice_amount", ""))
    except InvalidOperation:
        return f"row {n}: invoice_amount is not a number"
    if amount <= 0:
        return f"row {n}: invoice_amount must be more than 0"
    outstanding: Decimal | None = None
    if raw.get("outstanding_amount"):
        try:
            outstanding = _money(raw["outstanding_amount"])
        except InvalidOperation:
            return f"row {n}: outstanding_amount is not a number"
        if outstanding < 0 or outstanding > amount:
            return f"row {n}: outstanding_amount must be between 0 and invoice_amount"
    invoice_date: date | None = None
    if raw.get("invoice_date"):
        try:
            invoice_date = date.fromisoformat(raw["invoice_date"])
        except ValueError:
            return f"row {n}: invoice_date must be YYYY-MM-DD"
    currency = (raw.get("currency") or "INR").upper()
    if not _CURRENCY_RE.match(currency):
        return f"row {n}: currency must be a 3-letter code like INR"
    return _Row(number, raw.get("payer_name") or None, invoice_date, amount, currency, outstanding)


def _posted_settled(session: Session, invoice: Invoice, currency: str) -> Decimal:
    total = session.scalar(
        select(func.sum(InvoicePayment.settled)).where(
            InvoicePayment.invoice_id == invoice.id, InvoicePayment.currency == currency
        )
    )
    return Decimal(total or 0)


def import_open_invoices(session: Session, data: bytes) -> ImportResult:
    if len(data) > MAX_BYTES:
        raise CsvImportError("the file is larger than 1 MB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvImportError("the file isn't UTF-8 text — save it as CSV (UTF-8)") from None

    reader = csv.DictReader(io.StringIO(text))
    headers = {(h or "").strip().lower() for h in reader.fieldnames or []}
    missing = [c for c in REQUIRED if c not in headers]
    if missing:
        raise CsvImportError(f"missing column(s): {', '.join(missing)}")

    raw_rows: list[tuple[int, dict[str, str]]] = []
    for n, raw in enumerate(reader, start=2):
        if len(raw_rows) >= MAX_ROWS:
            raise CsvImportError("the file has more than 5,000 rows")
        cleaned = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        if not any(cleaned.values()):
            continue  # blank line
        raw_rows.append((n, cleaned))

    result = ImportResult()
    parsed: list[tuple[int, _Row]] = []
    for n, raw in raw_rows:
        row = _parse(n, raw)
        if isinstance(row, str):
            result.skipped.append(row)
        else:
            parsed.append((n, row))

    counts = Counter(number_key(r.number) for _, r in parsed)
    for n, row in parsed:
        if counts[number_key(row.number)] > 1:
            result.skipped.append(f"row {n}: {row.number} appears more than once in the file")
            continue
        existing = find_invoice(session, row.number)
        if existing is None:
            paid_before = (
                row.amount - row.outstanding if row.outstanding is not None else Decimal("0")
            )
            session.add(
                Invoice(
                    invoice_number=row.number,
                    number_key=number_key(row.number),
                    payer_name=row.payer_name,
                    invoice_date=row.invoice_date,
                    amount=row.amount,
                    currency=row.currency,
                    source="books",
                    paid_before_import=paid_before,
                )
            )
            result.imported += 1
            continue
        if existing.source == "email" and abs(existing.amount - row.amount) > TOLERANCE:
            existing.note = (
                f"email said {format_money(existing.amount, existing.currency)}, "
                f"your books say {format_money(row.amount, row.currency)}"
            )
        existing.invoice_number = row.number
        existing.payer_name = row.payer_name or existing.payer_name
        existing.invoice_date = row.invoice_date or existing.invoice_date
        existing.amount = row.amount
        existing.currency = row.currency
        existing.source = "books"
        if row.outstanding is not None:
            # the export's outstanding already reflects payments this pipeline
            # delivered, so only the remainder was paid outside it (spec §1).
            posted = _posted_settled(session, existing, row.currency)
            existing.paid_before_import = max(Decimal("0"), row.amount - row.outstanding - posted)
        result.updated += 1

    result.skipped.sort(key=lambda s: int(s.split()[1].rstrip(":")))
    session.flush()
    return result
