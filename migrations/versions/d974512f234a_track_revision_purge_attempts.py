"""Rotate failed storage deletions behind other due revisions."""

import sqlalchemy as sa
from alembic import op


revision = "d974512f234a"
down_revision = "3da42d92b172"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("activity_revisions", sa.Column("purge_attempted_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("activity_revisions", "purge_attempted_at")
