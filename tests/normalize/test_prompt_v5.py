from __future__ import annotations

from ar_pipeline.normalize.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_header_message,
    build_mapping_message,
    system_prompt_for,
)
from ar_pipeline.tables.mapping import find_line_table
from tests.tables.advice_pdf import build_advice_pdf


def _raws():
    from ar_pipeline.extract.pdf import extract_pdf

    return [extract_pdf(build_advice_pdf()).to_payload()]


def test_version_and_adjustment_wording():
    assert PROMPT_VERSION == "5"
    assert 'kind="adjustment"' in SYSTEM_PROMPT and "applies_to" in SYSTEM_PROMPT


def test_client_names_are_appended_only_when_set():
    assert system_prompt_for([]) == SYSTEM_PROMPT
    prompt = system_prompt_for(["Acme Metals", "Acme Metals Ltd"])
    assert prompt.startswith(SYSTEM_PROMPT)
    assert "Acme Metals, Acme Metals Ltd" in prompt and "never the payer" in prompt


def test_mapping_message_shows_columns_and_three_rows():
    table = find_line_table(_raws())
    msg = build_mapping_message(table)
    assert "0 = BillNo" in msg and "6 = Net Payment" in msg
    assert msg.count("\n2510004516DISCO") == 1 and "2510004515DISCO" not in msg


def test_header_message_elides_the_table():
    raws = _raws()
    table = find_line_table(raws)
    msg = build_header_message("ap@ourco.com", "FW: advice", raws, table)
    assert "CBB2510004583" in msg and "[... 17 more rows read separately ...]" in msg
    assert "2510031878DISCO" not in msg
    assert "Total | " in msg
