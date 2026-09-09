"""HTML-body extractor -- every ``<table>`` plus the visible page text."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from ar_pipeline.extract.base import ExtractedContent

_WS_RE = re.compile(r"\s+")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def _cell_text(cell: Tag) -> str:
    return _WS_RE.sub(" ", cell.get_text(" ", strip=True))


def extract_html_tables(body_html: str) -> ExtractedContent:
    soup = BeautifulSoup(body_html, "lxml")

    tables: list[list[list[str]]] = []
    for table in soup.find_all("table"):
        if not isinstance(table, Tag):
            continue
        # Skip an outer wrapper table that nests a <table>; the inner table is
        # emitted on its own, so iterating the wrapper's rows recursively would
        # duplicate the inner rows.
        if table.find("table"):
            continue
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            if not isinstance(tr, Tag):
                continue
            cells = [_cell_text(c) for c in tr.find_all(["td", "th"]) if isinstance(c, Tag)]
            rows.append(cells)
        tables.append(rows)

    for junk in soup(["script", "style"]):
        junk.decompose()
    text = _BLANK_RUN_RE.sub("\n\n", soup.get_text("\n"))

    return ExtractedContent(
        text=text,
        tables=tables,
        meta={"table_count": len(tables)},
    )
