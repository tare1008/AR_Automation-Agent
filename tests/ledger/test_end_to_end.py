"""The installment story from the spec: books invoice ₹1,00,000, paid 25k / 50k / 30k
through the real pipeline (fake LLM), ending overpaid by ₹5,000."""

from decimal import Decimal

from sqlalchemy import select

import ar_pipeline.config as config_module
from ar_pipeline.db.models import Extraction
from ar_pipeline.ledger.csv_import import import_open_invoices
from ar_pipeline.ledger.posting import find_invoice
from ar_pipeline.ledger.queries import invoice_detail
from ar_pipeline.normalize.service import normalize_one
from tests.ledger.test_routing import _extracted_email, _output
from tests.normalize.llm_fake import FakeLLMClient


def test_installments_end_overpaid(db_session, monkeypatch):
    monkeypatch.setenv("AUTO_APPROVE_MIN_CONFIDENCE", "0.75")
    config_module.get_settings.cache_clear()
    try:
        import_open_invoices(
            db_session, b"invoice_number,payer_name,invoice_amount\nINV-1,Acme Corp,100000\n"
        )
        statuses = []
        for i, amount in enumerate(("25000", "50000", "30000")):
            email = _extracted_email(db_session, f"m-e2e-{i}")
            normalize_one(db_session, email, FakeLLMClient(response=_output(amount)))
            ext = db_session.scalars(
                select(Extraction).where(Extraction.email_id == email.id)
            ).one()
            statuses.append(ext.status)
        # 25k and 50k are clean installments; 30k overpays and is held for review
        assert statuses == ["approved", "approved", "pending_review"]
        flags = ext.validation_flags
        assert any("overpaid by ₹5,000.00" in f for f in flags)

        # reviewer approves it anyway, having seen the flag
        from ar_pipeline.review.auth import User
        from ar_pipeline.review.service import save_edits

        form = {"header": ext.canonical["header"], "line_items": ext.canonical["line_items"]}
        save_edits(db_session, ext.id, User(name="Asha"), form, approve=True)

        inv = find_invoice(db_session, "INV-1")
        detail = invoice_detail(db_session, inv.id)
        assert detail.row.status == "overpaid"
        assert detail.row.outstanding == Decimal("-5000")
    finally:
        config_module.get_settings.cache_clear()
