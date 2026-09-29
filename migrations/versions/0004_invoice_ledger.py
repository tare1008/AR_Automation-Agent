"""invoice ledger: invoice + invoice_payment

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-29 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "invoice",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("invoice_number", sa.Text(), nullable=False),
        sa.Column("number_key", sa.Text(), nullable=False),
        sa.Column("payer_name", sa.Text(), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("paid_before_import", sa.Numeric(14, 2), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("source IN ('books', 'email')", name="ck_invoice_source"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("number_key", name="uq_invoice_number_key"),
    )
    op.create_table(
        "invoice_payment",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("invoice_id", sa.UUID(), nullable=False),
        sa.Column("extraction_id", sa.UUID(), nullable=False),
        sa.Column("line_index", sa.Integer(), nullable=False),
        sa.Column("amount_paid", sa.Numeric(14, 2), nullable=False),
        sa.Column("deductions_total", sa.Numeric(14, 2), nullable=False),
        sa.Column("settled", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payment_reference", sa.Text(), nullable=True),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["extraction_id"], ["extraction.id"]),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoice.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "extraction_id", "line_index", name="uq_invoice_payment_extraction_line"
        ),
    )
    op.create_index(
        op.f("ix_invoice_payment_invoice_id"), "invoice_payment", ["invoice_id"]
    )
    op.create_index(
        op.f("ix_invoice_payment_extraction_id"), "invoice_payment", ["extraction_id"]
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_invoice_payment_extraction_id"), table_name="invoice_payment"
    )
    op.drop_index(op.f("ix_invoice_payment_invoice_id"), table_name="invoice_payment")
    op.drop_table("invoice_payment")
    op.drop_table("invoice")
