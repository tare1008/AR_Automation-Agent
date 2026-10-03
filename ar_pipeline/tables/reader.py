"""Read a payment advice through its line table (spec §4.2): map the columns
once per header layout, apply the mapping to every row in code, ask the AI only
for the header, and accept the read only when it agrees with the document's
own totals. Returns None whenever the full-AI read should run instead."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from ar_pipeline.ledger.matching import payer_slug
from ar_pipeline.normalize.llm_client import LLMClient
from ar_pipeline.normalize.normalizer import (
    NormalizedPayment,
    NormalizerOutput,
    PaymentDraft,
    build_payments,
)
from ar_pipeline.normalize.prompt import (
    HEADER_SYSTEM_PROMPT,
    MAPPING_SYSTEM_PROMPT,
    build_header_message,
    build_mapping_message,
    system_prompt_for,
)
from ar_pipeline.tables.mapping import (
    ColumnMap,
    LineTable,
    MappedTable,
    MappingError,
    apply_mapping,
    find_line_table,
    header_signature,
    validate_mapping,
)
from ar_pipeline.tables.models import HeaderOutput, MappingOutput
from ar_pipeline.tables.store import discard_mapping, load_mapping, save_mapping, touch_mapping
from ar_pipeline.tables.totals import document_totals, totals_flags


@dataclass(frozen=True)
class TableRead:
    output: NormalizerOutput
    payments: list[NormalizedPayment]
    read_info: dict


def _learn(table: LineTable, llm_client: LLMClient) -> ColumnMap | None:
    out = llm_client.parse(
        system=MAPPING_SYSTEM_PROMPT, user=build_mapping_message(table), output_model=MappingOutput
    )
    return validate_mapping(out, table.header)


def _output(header: HeaderOutput, mapped: MappedTable) -> NormalizerOutput:
    total = sum((li.amount_paid for li in mapped.lines), Decimal("0"))
    draft = PaymentDraft(
        payer_name=header.payer_name or "",
        payer_id=header.payer_id,
        payment_reference=header.payment_reference,
        payment_reference_type=header.payment_reference_type,
        payment_date=header.payment_date,
        payment_method=header.payment_method,
        currency=header.currency,
        total_paid_amount=total,
        line_items=list(mapped.lines),
        vendor_guess=header.vendor_guess,
        confidence=header.confidence,
    )
    note = f"Rows read from the table in code ({len(mapped.lines)} lines)."
    notes = f"{header.notes} | {note}" if header.notes else note
    return NormalizerOutput(is_remittance=True, notes=notes, payments=[draft])


def read_by_table(
    session: Session,
    *,
    email_id: str,
    sender: str,
    subject: str,
    raws: list[dict],
    llm_client: LLMClient,
    client_names: list[str],
) -> TableRead | None:
    table = find_line_table(raws)
    if table is None:
        return None
    signature = header_signature(table.header)
    texts = [str(r.get("text") or "") for r in raws]
    saved = load_mapping(session, signature)
    header: HeaderOutput | None = None
    for origin in (["saved"] if saved is not None else []) + ["learned"]:
        cols = saved if origin == "saved" else _learn(table, llm_client)
        if cols is None:
            return None
        try:
            mapped = apply_mapping(table, cols)
        except MappingError:
            if origin == "saved":
                discard_mapping(session, signature)
                continue
            return None
        if header is None:
            header = llm_client.parse(
                system=system_prompt_for(client_names, HEADER_SYSTEM_PROMPT),
                user=build_header_message(sender, subject, raws, table),
                output_model=HeaderOutput,
            )
            if not header.is_remittance:
                return None
        out = _output(header, mapped)
        payments = build_payments(email_id, out)
        if len(payments) != 1:
            return None
        document = document_totals(mapped.column_totals, texts)
        if totals_flags(payments[0].payload, document):
            if origin == "saved":
                discard_mapping(session, signature)
                continue
            return None
        if origin == "learned":
            save_mapping(
                session,
                signature,
                table.header,
                cols,
                payer_slug(payments[0].payload.header.payer_name),
            )
        else:
            touch_mapping(session, signature)
        return TableRead(
            out, payments, {"path": "table", "mapping": origin, "document_totals": document}
        )
    return None
