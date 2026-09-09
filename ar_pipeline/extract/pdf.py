"""PDF extractor -- per-page text joined, per-page tables appended."""

from __future__ import annotations

from io import BytesIO

import pdfplumber

from ar_pipeline.extract.base import ExtractedContent


def extract_pdf(data: bytes) -> ExtractedContent:
    page_texts: list[str] = []
    tables: list[list[list[str]]] = []

    with pdfplumber.open(BytesIO(data)) as pdf:
        page_count = len(pdf.pages)
        for page in pdf.pages:
            page_texts.append(page.extract_text() or "")
            for table in page.extract_tables():
                tables.append(
                    [[str(cell) if cell is not None else "" for cell in row] for row in table]
                )

    return ExtractedContent(
        text="\n\n".join(page_texts),
        tables=tables,
        meta={"page_count": page_count},
    )
