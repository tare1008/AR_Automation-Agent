"""Plain-text body extractor -- line structure preserved, no tables."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ar_pipeline.extract.base import ExtractedContent

# 3+ consecutive blank lines (>= 4 newlines) collapse to a single blank line.
_BLANK_RUN_RE = re.compile(r"\n{4,}")


def extract_body_text(body_text: str, body_html: str) -> ExtractedContent:
    if body_text and body_text.strip():
        raw = body_text
        source = "body_text"
    else:
        raw = BeautifulSoup(body_html, "lxml").get_text("\n")
        source = "body_html"

    lines = [line.rstrip() for line in raw.splitlines()]
    text = _BLANK_RUN_RE.sub("\n\n", "\n".join(lines))

    return ExtractedContent(text=text, tables=[], meta={"source": source})
