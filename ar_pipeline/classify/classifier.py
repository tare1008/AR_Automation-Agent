"""Turn one ``Email`` + its ``Attachment`` rows into extraction-source specs.

The classifier decides *what* should be handed to an extractor and *how*:

* each attachment becomes at most one spec (``excel`` / ``pdf_text`` /
  ``pdf_scanned`` / ``image``), possibly ``skipped``;
* the email body becomes at most one spec: a ``body_table`` is always
  emitted when the body carries a numeric table (real signal even next to
  an attachment -- the normalizer dedups), while a prose ``body_text`` is
  emitted only when no non-skipped attachment source already exists, so a
  plain cover note for a PDF/spreadsheet is not re-extracted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO

import pdfplumber
from bs4 import BeautifulSoup

from ar_pipeline.db.models import Attachment, Email
from ar_pipeline.storage import BlobStore

_BODY_REF = "body"

_EXCEL_MARKERS = ("spreadsheetml", "excel", "ms-excel")
_EXCEL_EXTS = (".xlsx", ".xls")

_INLINE_IMAGE_RE = re.compile(r"(?i)(image\d+|logo|signature|outlook-)")
_NUMERIC_CELL_RE = re.compile(r"[\d,]+\.\d{2}")

_INLINE_IMAGE_MAX_SIZE = 25_000
_PDF_TEXT_MIN_CHARS = 20
_BODY_TEXT_MIN_CHARS = 120


@dataclass(frozen=True)
class SourceSpec:
    kind: str  # body_table | body_text | excel | pdf_text | pdf_scanned | image
    ref: str  # "body" or an attachment id string
    skipped: bool = False
    skip_reason: str | None = None


def pdf_has_text_layer(data: bytes) -> bool:
    """``True`` when the PDF carries a usable text layer.

    Concatenates ``page.extract_text()`` across every page; the PDF is
    considered text-bearing when that has >= 20 non-whitespace characters.
    """
    chunks: list[str] = []
    with pdfplumber.open(BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            chunks.append(text)
    joined = "".join(chunks)
    return len(re.sub(r"\s+", "", joined)) >= _PDF_TEXT_MIN_CHARS


def _non_ws_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _is_excel(content_type: str, filename: str) -> bool:
    ct = content_type.lower()
    name = filename.lower()
    return any(m in ct for m in _EXCEL_MARKERS) or name.endswith(_EXCEL_EXTS)


def _is_pdf(content_type: str, filename: str) -> bool:
    ct = content_type.lower()
    if ct == "application/pdf":
        return True
    return ct == "application/octet-stream" and filename.lower().endswith(".pdf")


def _blob_key(att: Attachment) -> str:
    return f"{att.email_id}/{att.id}/{att.filename}"


def _classify_attachment(att: Attachment, blob_store: BlobStore) -> SourceSpec:
    ref = str(att.id)
    content_type = att.content_type or ""
    filename = att.filename or ""

    if _is_excel(content_type, filename):
        return SourceSpec("excel", ref)

    if _is_pdf(content_type, filename):
        data = blob_store.get(_blob_key(att))
        kind = "pdf_text" if pdf_has_text_layer(data) else "pdf_scanned"
        return SourceSpec(kind, ref)

    if content_type.lower().startswith("image/"):
        below_threshold = att.size < _INLINE_IMAGE_MAX_SIZE and bool(
            _INLINE_IMAGE_RE.search(filename)
        )
        if below_threshold:
            return SourceSpec(
                "image", ref, skipped=True, skip_reason="inline image below threshold"
            )
        return SourceSpec("image", ref)

    # kind="image" is cosmetic here — skipped=True means it is never handed to an
    # extractor; the CHECK constraint has no "unknown" value.
    return SourceSpec(
        "image",
        ref,
        skipped=True,
        skip_reason=f"unsupported attachment type {content_type}",
    )


def _body_text(email: Email) -> str:
    if email.body_text and email.body_text.strip():
        return email.body_text
    if email.body_html:
        return BeautifulSoup(email.body_html, "lxml").get_text(" ")
    return ""


def _has_numeric_table(body_html: str) -> bool:
    if not body_html:
        return False
    soup = BeautifulSoup(body_html, "lxml")
    for table in soup.find_all("table"):
        cells = table.find_all(["td", "th"])
        numeric = sum(1 for c in cells if _NUMERIC_CELL_RE.search(c.get_text()))
        if numeric >= 2:
            return True
    return False


def _classify_body(email: Email, has_live_attachment_source: bool) -> SourceSpec | None:
    if _has_numeric_table(email.body_html):
        return SourceSpec("body_table", _BODY_REF)

    if has_live_attachment_source:
        return None  # a prose cover note alongside an attachment is not a source

    text = _body_text(email)
    if _non_ws_len(text) >= _BODY_TEXT_MIN_CHARS and any(ch.isdigit() for ch in text):
        return SourceSpec("body_text", _BODY_REF)

    return None


def classify_email(
    email: Email, attachments: list[Attachment], blob_store: BlobStore
) -> list[SourceSpec]:
    specs: list[SourceSpec] = [_classify_attachment(att, blob_store) for att in attachments]

    has_live_attachment_source = any(not s.skipped for s in specs)
    body_spec = _classify_body(email, has_live_attachment_source)
    if body_spec is not None:
        specs.append(body_spec)

    if not any(not s.skipped for s in specs):
        specs.append(
            SourceSpec(
                "body_text",
                _BODY_REF,
                skipped=True,
                skip_reason="no extractable content",
            )
        )

    return specs
