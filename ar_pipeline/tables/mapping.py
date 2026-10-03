"""Find the one line table in a message's sources, map its columns to
line-item fields, and apply the mapping to every row in code — figures copied
exactly, any number of rows (spec §4.2). Pure — no DB, no network."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from ar_pipeline.schema.canonical import Deduction, LineItem
from ar_pipeline.tables.models import ColumnAssignment, MappingOutput
from ar_pipeline.tables.numbers import parse_amount, parse_date

MIN_DATA_ROWS = 6
_REQUIRED = {"invoice_number", "amount_paid"}
_TOTAL_RE = re.compile(r"^\s*(?:grand\s*)?total\b|^\s*net\s*payable", re.I)
_ZERO = Decimal("0")

ColumnMap = dict[str, int]


class MappingError(ValueError):
    """The mapping does not fit this table; use the full-AI read."""


@dataclass(frozen=True)
class LineTable:
    source_index: int
    table_index: int
    header: list[str]
    rows: list[list[str]]
    total_row: list[str] | None


@dataclass(frozen=True)
class MappedTable:
    lines: list[LineItem]
    column_totals: dict[str, Decimal]


def _compact(cell: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(cell).lower())


def header_signature(header: list[str]) -> str:
    return hashlib.sha256("|".join(_compact(c) for c in header).encode()).hexdigest()


def _numeric_share(rows: list[list[str]], col: int) -> float:
    hits = sum(1 for r in rows if col < len(r) and parse_amount(r[col]) is not None)
    return hits / len(rows) if rows else 0.0


def find_line_table(raws: list[dict]) -> LineTable | None:
    found: list[LineTable] = []
    for si, raw in enumerate(raws):
        for ti, table in enumerate(raw.get("tables") or []):
            rows = [[str(c) for c in r] for r in table if any(str(c).strip() for c in r)]
            if len(rows) < 2 or len(rows[0]) < 3:
                continue
            header, body = rows[0], rows[1:]
            total = None
            if any(_TOTAL_RE.match(c) for c in body[-1] if c):
                total, body = body[-1], body[:-1]
            if len(body) < MIN_DATA_ROWS:
                continue
            numeric = sum(1 for i in range(len(header)) if _numeric_share(body, i) >= 0.5)
            if numeric < 2:
                continue
            found.append(LineTable(si, ti, header, body, total))
    return found[0] if len(found) == 1 else None


_RULES: list[tuple[str, Callable[[str], bool]]] = [
    ("tds", lambda c: "tds" in c),
    ("invoice_date", lambda c: "date" in c),
    ("payment_reference", lambda c: any(w in c for w in ("utr", "rtgs", "neft"))),
    (
        "invoice_number",
        lambda c: (
            c
            in {
                "invoice",
                "bill",
                "billno",
                "billnumber",
                "invoiceno",
                "invoicenumber",
                "invno",
                "docno",
                "documentno",
            }
        ),
    ),
    (
        "invoice_amount",
        lambda c: "gross" in c or c in {"invoiceamount", "billamount", "invoicevalue"},
    ),
    (
        "adjustment",
        lambda c: c.startswith("adv") or any(w in c for w in ("debit", "adjust", "discount")),
    ),
    (
        "amount_paid",
        lambda c: (
            c.startswith("net")
            or c in {"amountpaid", "paidamount", "payment", "paymentamount", "amount"}
        ),
    ),
]


def keyword_mapping(header: list[str]) -> ColumnMap | None:
    """The offline mapper: each column gets the first matching role, each role
    its first column. None when invoice number or amount paid is missing."""
    cols: ColumnMap = {}
    for i, cell in enumerate(header):
        c = _compact(cell)
        if not c:
            continue
        for role, rule in _RULES:
            if role not in cols and rule(c):
                cols[role] = i
                break
    return cols if cols.keys() >= _REQUIRED else None


def mapping_output(cols: ColumnMap | None) -> MappingOutput:
    if cols is None:
        return MappingOutput(is_line_table=False)
    return MappingOutput(
        is_line_table=True,
        columns=[ColumnAssignment(index=i, role=r) for r, i in cols.items()],
    )


def validate_mapping(out: MappingOutput, header: list[str]) -> ColumnMap | None:
    if not out.is_line_table:
        return None
    cols: ColumnMap = {}
    for a in out.columns:
        if a.role == "ignore":
            continue
        if not 0 <= a.index < len(header) or a.role in cols or a.index in cols.values():
            return None
        cols[a.role] = a.index
    return cols if cols.keys() >= _REQUIRED else None


def _get(row: list[str], cols: ColumnMap, role: str) -> str:
    i = cols.get(role)
    return row[i].strip() if i is not None and i < len(row) else ""


def _abs(raw: str) -> Decimal:
    value = parse_amount(raw)
    return abs(value) if value is not None else _ZERO


def apply_mapping(table: LineTable, cols: ColumnMap) -> MappedTable:
    if "payment_reference" in cols:
        refs = {_get(r, cols, "payment_reference") for r in table.rows} - {""}
        if len(refs) > 1:
            raise MappingError("rows carry different bank references — several payments")
    lines: list[LineItem] = []
    for row in table.rows:
        number = _get(row, cols, "invoice_number")
        gross = parse_amount(_get(row, cols, "invoice_amount"))
        paid = parse_amount(_get(row, cols, "amount_paid"))
        tds = _abs(_get(row, cols, "tds"))
        adj = _abs(_get(row, cols, "adjustment"))
        other = _abs(_get(row, cols, "other_deduction"))
        if gross is None and paid is None and adj == 0:
            continue  # a heading or note row
        day = parse_date(_get(row, cols, "invoice_date"))
        if (paid is not None and paid < 0) or ((gross is None or gross == 0) and adj > 0):
            amount = adj if adj > 0 else abs(paid if paid is not None else gross or _ZERO)
            if amount == 0:
                continue
            kind = "discount" if "DISC" in number.upper() else "debit_note"
            lines.append(
                LineItem(
                    invoice_number=number,
                    invoice_date=day,
                    invoice_amount=_ZERO,
                    deductions=[Deduction(type=kind, amount=amount)],
                    amount_paid=-amount,
                    kind="adjustment",
                )
            )
            continue
        deductions = [
            Deduction(type=t, amount=a)
            for t, a in (("tds", tds), ("advance_adjustment", adj), ("other", other))
            if a > 0
        ]
        total_ded = sum((d.amount for d in deductions), _ZERO)
        invoice_amount = gross if gross is not None else (paid or _ZERO) + total_ded
        amount_paid = paid if paid is not None else invoice_amount - total_ded
        lines.append(
            LineItem(
                invoice_number=number,
                invoice_date=day,
                invoice_amount=invoice_amount,
                deductions=deductions,
                amount_paid=amount_paid,
            )
        )
    if not lines:
        raise MappingError("no line rows")
    totals: dict[str, Decimal] = {}
    if table.total_row is not None:
        for role in ("invoice_amount", "tds", "adjustment", "amount_paid"):
            value = parse_amount(_get(table.total_row, cols, role))
            if value is not None:
                totals[role] = abs(value) if role in ("tds", "adjustment") else value
    return MappedTable(lines, totals)
