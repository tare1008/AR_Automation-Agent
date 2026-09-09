"""Deterministic content extractors.

Each extractor is a pure function turning one source (spreadsheet bytes,
email HTML, plain-text body, PDF bytes) into an :class:`ExtractedContent`.
"""

from __future__ import annotations

from ar_pipeline.extract.base import EXTRACTOR_VERSION, ExtractedContent
from ar_pipeline.extract.body_text import extract_body_text
from ar_pipeline.extract.excel import extract_excel
from ar_pipeline.extract.html_table import extract_html_tables
from ar_pipeline.extract.pdf import extract_pdf

__all__ = [
    "EXTRACTOR_VERSION",
    "ExtractedContent",
    "extract_body_text",
    "extract_excel",
    "extract_html_tables",
    "extract_pdf",
]
