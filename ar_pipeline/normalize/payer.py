"""Payer vs beneficiary guard (spec §4.5). Pure."""

from __future__ import annotations

from ar_pipeline.ledger.matching import payer_slug, payers_differ
from ar_pipeline.schema.canonical import RemittancePayload

FLAG_PAYER_IS_CLIENT = "header: payer looks like the receiving company — check who paid"


def payer_flags(payload: RemittancePayload, client_names: list[str]) -> list[str]:
    payer = payload.header.payer_name
    if not payer_slug(payer):
        return []
    for name in client_names:
        if payer_slug(name) and not payers_differ(payer, name):
            return [FLAG_PAYER_IS_CLIENT]
    return []
