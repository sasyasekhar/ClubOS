"""Require positive scan evidence before an attachment can be downloaded."""

import sqlalchemy as sa
from alembic import op


revision = "9c9b83ccaf31"
down_revision = "2df07cec79a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("activity_attachments", sa.Column("scan_passed_at", sa.DateTime(timezone=True)))
    op.create_check_constraint("ck_attachment_scan_metadata", "activity_attachments",
                               "scan_passed_at IS NULL OR content_sha256 IS NOT NULL")


def downgrade() -> None:
    op.drop_constraint("ck_attachment_scan_metadata", "activity_attachments", type_="check")
    op.drop_column("activity_attachments", "scan_passed_at")
