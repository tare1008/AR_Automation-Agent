"""Pure formatting helper for the live normalization eval.

``summarise`` turns a ``NormalizerOutput`` plus the per-payment validator flags
into a compact human-readable block. No I/O — it is unit-tested directly and
also called by the gated ``test_live_normalization_over_fixtures``.
"""

from __future__ import annotations

from ar_pipeline.normalize.normalizer import NormalizerOutput


def summarise(
    email_name: str,
    out: NormalizerOutput,
    flags_per_payment: list[list[str]],
) -> str:
    """Render a compact report block for one email's normalization result.

    ``flags_per_payment[i]`` holds the validator flags for ``out.payments[i]``
    (an empty list means clean). A shorter list than ``out.payments`` is
    tolerated — missing entries are treated as clean.
    """
    lines: list[str] = [
        f"=== {email_name} ===",
        f"is_remittance: {out.is_remittance}   payments: {len(out.payments)}",
        f"notes: {out.notes or '-'}",
    ]
    for i, payment in enumerate(out.payments):
        flags = flags_per_payment[i] if i < len(flags_per_payment) else []
        lines.append(
            f"  payment {i}: {payment.payer_name} "
            f"| ref {payment.payment_reference or '-'} "
            f"| total {payment.total_paid_amount} "
            f"| conf {payment.confidence} "
            f"| {len(payment.line_items)} lines"
        )
        lines.append(f"    flags: {'; '.join(flags) if flags else 'clean'}")
    return "\n".join(lines)
