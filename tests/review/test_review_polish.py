from __future__ import annotations


def test_queue_shows_payer_and_amount(client, seed_pending):
    seed_pending()
    page = client.get("/review/queue").text
    assert "Acme Corp" in page
    assert "₹90.00" in page


def test_detail_has_summary_plain_labels_and_labelled_deductions(client, seed_pending):
    _email, ext = seed_pending()
    page = client.get(f"/review/{ext.id}").text
    assert 'class="summary"' in page and "Total paid" in page and "1 invoice" in page
    assert ">PAYER_NAME<" not in page and "payment_reference_type\n" not in page
    assert "Reference type" in page and "Amount paid" in page
    assert '<option value="tds" selected>TDS</option>' in page
    assert 'id="dedrowTemplate"' in page  # "+ deduction" works with no existing row


def test_flags_render_as_a_list_without_the_header_prefix(client, db_session, seed_pending):
    _email, ext = seed_pending()
    ext.validation_flags = ["header: payer looks like the receiving company — check who paid"]
    db_session.flush()
    page = client.get(f"/review/{ext.id}").text
    assert '<ul class="flag-list" role="alert"><li>payer looks like the receiving company' in page


def test_buttons_keep_their_own_colours():
    from pathlib import Path

    css = (Path(__file__).parents[2] / "ar_pipeline/review/static/review.css").read_text()
    assert 'button[type="submit"], .btn-primary' not in css
    assert 'button:where([type="submit"]), .btn-primary' in css
