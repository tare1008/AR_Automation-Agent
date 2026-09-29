from decimal import Decimal

from ar_pipeline.db.models import Extraction
from tests.review.conftest import _canonical


def test_email_view_hides_payment_column_for_a_single_payment_email(client, seed_pending):
    email, _ext = seed_pending()
    r = client.get(f"/review/email/{email.id}")
    assert r.status_code == 200
    assert "Payment</th>" not in r.text
    assert "#0" not in r.text


def test_email_view_shows_payment_column_for_a_multi_payment_email(
    client, db_session, seed_pending
):
    email, _ext = seed_pending(canonical=_canonical(payment_index=0))
    second = Extraction(
        email_id=email.id,
        canonical=_canonical(payment_index=1),
        confidence=Decimal("0.7"),
        is_remittance=True,
        validation_flags=[],
        llm_model="claude-opus-5",
        prompt_version="2",
        status="pending_review",
    )
    db_session.add(second)
    db_session.flush()

    r = client.get(f"/review/email/{email.id}")
    assert r.status_code == 200
    assert "Payment</th>" in r.text
    assert "#0" in r.text
    assert "#1" in r.text
