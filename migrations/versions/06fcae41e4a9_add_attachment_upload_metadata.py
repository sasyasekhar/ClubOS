"""Add private attachment upload metadata and idempotency."""

import sqlalchemy as sa
from alembic import op


revision = "06fcae41e4a9"
down_revision = "27a850ee52c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing metadata has no trustworthy content size or hash to backfill.
    op.add_column("activity_attachments", sa.Column("idempotency_key", sa.Uuid()))
    op.add_column("activity_attachments", sa.Column("size_bytes", sa.Integer()))
    op.add_column("activity_attachments", sa.Column("content_sha256", sa.String(64)))
    op.create_unique_constraint(
        "uq_attachment_revision_idempotency",
        "activity_attachments",
        ["revision_id", "idempotency_key"],
    )
    op.create_check_constraint(
        "ck_attachment_upload_metadata",
        "activity_attachments",
        """(
            idempotency_key IS NULL AND size_bytes IS NULL AND content_sha256 IS NULL
        ) OR (
            idempotency_key IS NOT NULL
            AND size_bytes IS NOT NULL AND size_bytes BETWEEN 1 AND 10485760
            AND content_sha256 IS NOT NULL AND content_sha256 ~ '^[0-9a-f]{64}$'
        )""",
    )
    op.create_index(
        "ix_activity_attachments_storage_key", "activity_attachments", ["storage_key"]
    )


def downgrade() -> None:
    op.drop_index("ix_activity_attachments_storage_key", table_name="activity_attachments")
    op.drop_constraint("ck_attachment_upload_metadata", "activity_attachments", type_="check")
    op.drop_constraint("uq_attachment_revision_idempotency", "activity_attachments", type_="unique")
    op.drop_column("activity_attachments", "content_sha256")
    op.drop_column("activity_attachments", "size_bytes")
    op.drop_column("activity_attachments", "idempotency_key")
