"""PDF extractor -- page text outside ruled tables, plus the tables themselves,
cleaned the way payment advices need (spec §4.1): wrapped cells rejoined,
a table continued on the next page under the same header merged into one, and
a one-word continuation row at the top of a page glued back onto the row it
belongs to."""

from __future__ import annotations

import re
from io import BytesIO

import pdfplumber

from ar_pipeline.extract.base import ExtractedContent
from ar_pipeline.tables.numbers import parse_amount

Table = list[list[str]]


def _glue(a: str, b: str) -> str:
    """'WBBEL2510004' + '583' -> 'WBBEL2510004583'; fragments with spaces keep one."""
    if not a:
        return b
    if not b:
        return a
    return f"{a} {b}" if " " in a or " " in b else a + b


def _cell(raw: object) -> str:
    out = ""
    for part in str(raw or "").split("\n"):
        out = _glue(out, part.strip())
    return out


_TOTAL_LABEL = re.compile(r"(?i)^(grand\s*)?total$|^net\s*payable$")


def _is_continuation(row: list[str], prev: list[str]) -> bool:
    filled = [(i, c) for i, c in enumerate(row) if c]
    if not filled or len(filled) * 3 > len(row):
        return False
    return all(
        " " not in c
        and len(c) <= 12
        and i < len(prev)
        and prev[i]
        and parse_amount(prev[i]) is None
        and not _TOTAL_LABEL.match(c)
        for i, c in filled
    )


def _merge(tables: list[Table]) -> list[Table]:
    merged: list[Table] = []
    for table in tables:
        if merged and table and merged[-1] and table[0] == merged[-1][0]:
            rest = table[1:]
            if rest and len(merged[-1]) > 1 and _is_continuation(rest[0], merged[-1][-1]):
                prev = list(merged[-1][-1])
                merged[-1][-1] = prev
                for i, cell in enumerate(rest[0]):
                    if cell and i < len(prev):
                        prev[i] = _glue(prev[i], cell)
                rest = rest[1:]
            merged[-1].extend(rest)
        else:
            merged.append([list(r) for r in table])
    return merged


def extract_pdf(data: bytes) -> ExtractedContent:
    page_texts: list[str] = []
    tables: list[Table] = []

    with pdfplumber.open(BytesIO(data)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            found = page.find_tables()
            outside = page
            for table in found:
                outside = outside.outside_bbox(table.bbox, strict=False)
            page_texts.append(outside.extract_text() or "")
            for table in found:
                rows = [[_cell(c) for c in row] for row in table.extract()]
                rows = [row for row in rows if any(row)]
                if rows:
                    tables.append(rows)

    merged = _merge(tables)
    return ExtractedContent(
        text="\n\n".join(page_texts),
        tables=merged,
        meta={"page_count": page_count, "table_count": len(merged)},
    )
