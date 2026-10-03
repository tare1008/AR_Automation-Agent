"""multi-invoice PDFs: adjustment ledger rows, saved column mappings, read info"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoice_payment",
        sa.Column("kind", sa.String(length=12), server_default="payment", nullable=False),
    )
    op.create_check_constraint(
        "ck_invoice_payment_kind", "invoice_payment", "kind IN ('payment', 'adjustment')"
    )
    op.add_column(
        "extraction",
        sa.Column("read_info", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_table(
        "column_mapping",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column("header", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("columns", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payer_slug", sa.Text(), nullable=True),
        sa.Column("uses", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("signature", name="uq_column_mapping_signature"),
    )


def downgrade() -> None:
    op.drop_table("column_mapping")
    op.drop_column("extraction", "read_info")
    op.drop_constraint("ck_invoice_payment_kind", "invoice_payment", type_="check")
    op.drop_column("invoice_payment", "kind")
