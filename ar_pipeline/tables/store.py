"""Saved column mappings, one per table header layout (spec §4.2)."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ar_pipeline.db.models import ColumnMapping
from ar_pipeline.tables.mapping import ColumnMap


def load_mapping(session: Session, signature: str) -> ColumnMap | None:
    columns = session.scalar(
        select(ColumnMapping.columns).where(ColumnMapping.signature == signature)
    )
    return {str(k): int(v) for k, v in columns.items()} if columns else None


def save_mapping(
    session: Session, signature: str, header: list[str], cols: ColumnMap, payer_slug: str
) -> None:
    values = {
        "header": list(header),
        "columns": dict(cols),
        "payer_slug": payer_slug or None,
        "uses": 1,
        "last_used_at": func.now(),
    }
    session.execute(
        pg_insert(ColumnMapping)
        .values(id=uuid.uuid4(), signature=signature, **values)
        .on_conflict_do_update(index_elements=["signature"], set_=values)
    )
    session.flush()


def touch_mapping(session: Session, signature: str) -> None:
    session.execute(
        update(ColumnMapping)
        .where(ColumnMapping.signature == signature)
        .values(uses=ColumnMapping.uses + 1, last_used_at=func.now())
    )
    session.flush()


def discard_mapping(session: Session, signature: str) -> None:
    session.execute(delete(ColumnMapping).where(ColumnMapping.signature == signature))
    session.flush()
