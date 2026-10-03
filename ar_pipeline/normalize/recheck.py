"""Every flag a payment carries, recomputed the same way wherever it is
(re)checked: at normalization, on a reviewer's save, and after a CSV import.
Flags that only the original read can know (history, truncation, dropped
drafts, and duplicate warnings while the key is unchanged) are kept."""

from __future__ import annotations

import re

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ar_pipeline.config import get_settings
from ar_pipeline.db.models import Extraction
from ar_pipeline.ledger.checks import check_against_ledger
from ar_pipeline.normalize.payer import payer_flags
from ar_pipeline.normalize.validators import validate_payload
from ar_pipeline.schema.canonical import RemittancePayload
from ar_pipeline.tables.totals import totals_flags
from ar_pipeline.threads.dedupe import FLAG_HISTORICAL, KEY_FLAG_PREFIXES

TRUNCATED_FLAG = "header: content was truncated — check nothing is missing"
_DRAFT_FLAG_RE = re.compile(r"^draft\s+\d+:")


def sticky_flags(flags: list[str], *, key_changed: bool) -> list[str]:
    return [
        f
        for f in flags
        if f == TRUNCATED_FLAG
        or f.startswith(FLAG_HISTORICAL)
        or _DRAFT_FLAG_RE.match(f)
        or (not key_changed and f.startswith(KEY_FLAG_PREFIXES))
    ]


def context_flags(session: Session, ext: Extraction, payload: RemittancePayload) -> list[str]:
    document = (ext.read_info or {}).get("document_totals")
    return (
        totals_flags(payload, document)
        + payer_flags(payload, get_settings().client_name_list())
        + check_against_ledger(session, payload)
    )


def recheck(
    session: Session, ext: Extraction, payload: RemittancePayload, *, key_changed: bool
) -> list[str]:
    kept = sticky_flags(list(ext.validation_flags or []), key_changed=key_changed)
    fresh = validate_payload(payload) + context_flags(session, ext, payload)
    return kept + [f for f in fresh if f not in kept]


def refresh_pending(session: Session) -> int:
    """Recompute the flags of every pending remittance (after a CSV import or a
    backfill). Returns how many rows changed."""
    changed = 0
    pending = session.scalars(
        select(Extraction).where(
            Extraction.status == "pending_review", Extraction.is_remittance.is_(True)
        )
    )
    for ext in pending:
        try:
            payload = RemittancePayload.model_validate(ext.canonical or {})
        except ValidationError:
            continue
        new = recheck(session, ext, payload, key_changed=False)
        if new != list(ext.validation_flags or []):
            ext.validation_flags = new
            changed += 1
    session.flush()
    return changed
