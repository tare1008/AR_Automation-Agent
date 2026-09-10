from ar_pipeline.review.forms import canonical_diff, parse_form_to_canonical


def test_parse_builds_nested_header_and_lines():
    form = {
        "header.payer_name": "Acme Corp",
        "header.currency": "INR",
        "header.total_paid_amount": "1000.00",
        "header.payment_reference": "",
        "line_items[0].invoice_number": "INV-1",
        "line_items[0].invoice_amount": "600.00",
        "line_items[0].amount_paid": "600.00",
        "line_items[1].invoice_number": "INV-2",
        "line_items[1].invoice_amount": "400.00",
        "line_items[1].amount_paid": "400.00",
    }
    out = parse_form_to_canonical(form)
    assert out["header"]["payer_name"] == "Acme Corp"
    assert "payment_reference" not in out["header"]  # empty string dropped
    assert [li["invoice_number"] for li in out["line_items"]] == ["INV-1", "INV-2"]


def test_parse_nested_deductions():
    form = {
        "header.payer_name": "Acme",
        "header.total_paid_amount": "90",
        "line_items[0].invoice_number": "INV-1",
        "line_items[0].invoice_amount": "100",
        "line_items[0].amount_paid": "90",
        "line_items[0].deductions[0].type": "tds",
        "line_items[0].deductions[0].amount": "10",
        "line_items[0].deductions[0].reason": "194Q",
    }
    out = parse_form_to_canonical(form)
    assert out["line_items"][0]["deductions"] == [{"type": "tds", "amount": "10", "reason": "194Q"}]


def test_parse_compacts_sparse_indices_after_a_delete():
    form = {
        "header.payer_name": "Acme",
        "header.total_paid_amount": "5",
        "line_items[0].invoice_number": "INV-1",
        "line_items[0].invoice_amount": "5",
        "line_items[0].amount_paid": "5",
        "line_items[2].invoice_number": "INV-3",  # row 1 was deleted client-side
        "line_items[2].invoice_amount": "5",
        "line_items[2].amount_paid": "5",
    }
    out = parse_form_to_canonical(form)
    assert [li["invoice_number"] for li in out["line_items"]] == ["INV-1", "INV-3"]


def test_diff_reports_changed_scalar():
    old = {"header": {"payer_name": "Acme", "total_paid_amount": "100"}, "line_items": []}
    new = {"header": {"payer_name": "Acme Corp", "total_paid_amount": "100"}, "line_items": []}
    assert canonical_diff(old, new) == [("header.payer_name", "Acme", "Acme Corp")]


def test_diff_reports_added_and_removed_line_item():
    old = {"header": {}, "line_items": [{"invoice_number": "INV-1"}]}
    new = {"header": {}, "line_items": [{"invoice_number": "INV-1"}, {"invoice_number": "INV-2"}]}
    added = canonical_diff(old, new)
    assert ("line_items[1].invoice_number", None, "INV-2") in added

    removed = canonical_diff(new, old)
    assert ("line_items[1].invoice_number", "INV-2", None) in removed


def test_diff_empty_when_identical():
    doc = {"header": {"payer_name": "Acme"}, "line_items": [{"invoice_number": "INV-1"}]}
    assert canonical_diff(doc, doc) == []


def test_diff_normalises_numeric_strings_vs_numbers():
    old = {"header": {"total_paid_amount": 100}, "line_items": []}
    new = {"header": {"total_paid_amount": "100"}, "line_items": []}
    # 100 and "100" both normalize through json.dumps to "100", so no diff:
    # the point of this test is to PIN the behaviour: they are treated as equal
    assert canonical_diff(old, new) == []
