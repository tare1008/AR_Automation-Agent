"""add body_text kind

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_extraction_source_kind", "extraction_source", type_="check")
    op.create_check_constraint(
        "ck_extraction_source_kind",
        "extraction_source",
        "kind IN ('body_table','body_text','excel','pdf_text','pdf_scanned','image')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_extraction_source_kind", "extraction_source", type_="check")
    op.create_check_constraint(
        "ck_extraction_source_kind",
        "extraction_source",
        "kind IN ('body_table','excel','pdf_text','pdf_scanned','image')",
    )
