"""email threads: email_message, payment keys, history statuses"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "status IN ('pending_review', 'approved', 'rejected', 'superseded')"
_NEW = (
    "status IN ('pending_review', 'approved', 'rejected', 'superseded', "
    "'already_recorded', 'duplicate')"
)
_UNIQUE_WHERE = (
    "payment_key_strength = 'strong' AND "
    "status IN ('pending_review', 'approved', 'already_recorded')"
)


def upgrade() -> None:
    op.add_column("email", sa.Column("thread_key", sa.Text(), nullable=True))
    op.create_table(
        "email_message",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("sender", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_header", sa.Text(), nullable=False),
        sa.Column("is_internal", sa.Boolean(), nullable=False),
        sa.Column("carries_attachments", sa.Boolean(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("tables", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("seen_reason", sa.String(length=30), nullable=True),
        sa.Column("seen_in_message_id", sa.UUID(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('new', 'seen', 'no_content', 'failed')", name="ck_email_message_status"
        ),
        sa.ForeignKeyConstraint(["email_id"], ["email.id"]),
        sa.ForeignKeyConstraint(["seen_in_message_id"], ["email_message.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email_id", "position", name="uq_email_message_position"),
    )
    op.create_index(op.f("ix_email_message_email_id"), "email_message", ["email_id"])
    op.create_index(op.f("ix_email_message_fingerprint"), "email_message", ["fingerprint"])

    op.add_column("extraction_source", sa.Column("email_message_id", sa.UUID(), nullable=True))
    op.create_foreign_key(None, "extraction_source", "email_message", ["email_message_id"], ["id"])
    op.create_index(
        op.f("ix_extraction_source_email_message_id"), "extraction_source", ["email_message_id"]
    )

    op.add_column("extraction", sa.Column("email_message_id", sa.UUID(), nullable=True))
    op.add_column("extraction", sa.Column("payment_key", sa.Text(), nullable=True))
    op.add_column(
        "extraction", sa.Column("payment_key_strength", sa.String(length=10), nullable=True)
    )
    op.add_column("extraction", sa.Column("historical_reason", sa.String(length=30), nullable=True))
    op.add_column("extraction", sa.Column("duplicate_of_id", sa.UUID(), nullable=True))
    op.create_foreign_key(None, "extraction", "email_message", ["email_message_id"], ["id"])
    op.create_foreign_key(None, "extraction", "extraction", ["duplicate_of_id"], ["id"])
    op.create_index(op.f("ix_extraction_email_message_id"), "extraction", ["email_message_id"])
    op.create_index("ix_extraction_payment_key", "extraction", ["payment_key"])
    # every payment_key is NULL right now, so the unique index cannot fail;
    # `ar-pipeline threads-backfill` assigns keys afterwards, skipping conflicts.
    op.create_index(
        "ux_extraction_payment_key",
        "extraction",
        ["payment_key"],
        unique=True,
        postgresql_where=sa.text(_UNIQUE_WHERE),
    )
    op.drop_constraint("ck_extraction_status", "extraction", type_="check")
    op.create_check_constraint("ck_extraction_status", "extraction", _NEW)


def downgrade() -> None:
    # Forward-only assumption: fails if already_recorded/duplicate rows exist.
    op.drop_constraint("ck_extraction_status", "extraction", type_="check")
    op.create_check_constraint("ck_extraction_status", "extraction", _OLD)
    op.drop_index("ux_extraction_payment_key", table_name="extraction")
    op.drop_index("ix_extraction_payment_key", table_name="extraction")
    op.drop_index(op.f("ix_extraction_email_message_id"), table_name="extraction")
    op.drop_constraint("extraction_duplicate_of_id_fkey", "extraction", type_="foreignkey")
    op.drop_constraint("extraction_email_message_id_fkey", "extraction", type_="foreignkey")
    for col in (
        "duplicate_of_id",
        "historical_reason",
        "payment_key_strength",
        "payment_key",
        "email_message_id",
    ):
        op.drop_column("extraction", col)
    op.drop_index(op.f("ix_extraction_source_email_message_id"), table_name="extraction_source")
    op.drop_constraint(
        "extraction_source_email_message_id_fkey", "extraction_source", type_="foreignkey"
    )
    op.drop_column("extraction_source", "email_message_id")
    op.drop_index(op.f("ix_email_message_fingerprint"), table_name="email_message")
    op.drop_index(op.f("ix_email_message_email_id"), table_name="email_message")
    op.drop_table("email_message")
    op.drop_column("email", "thread_key")
