"""review UI columns: extraction.reject_reason + superseded status

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "status IN ('pending_review', 'approved', 'rejected')"
_NEW = "status IN ('pending_review', 'approved', 'rejected', 'superseded')"


def upgrade() -> None:
    op.add_column("extraction", sa.Column("reject_reason", sa.Text(), nullable=True))
    op.drop_constraint("ck_extraction_status", "extraction", type_="check")
    op.create_check_constraint("ck_extraction_status", "extraction", _NEW)


def downgrade() -> None:
    # Forward-only assumption: fails if any status='superseded' rows exist.
    op.drop_constraint("ck_extraction_status", "extraction", type_="check")
    op.create_check_constraint("ck_extraction_status", "extraction", _OLD)
    op.drop_column("extraction", "reject_reason")
