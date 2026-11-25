"""Preserve replay protection independently of audit retention."""

import sqlalchemy as sa
from alembic import op


revision = "3da42d92b172"
down_revision = "fe7b134d822d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operation_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("fingerprint", sa.LargeBinary()),
        sa.Column("result_id", sa.Uuid()),
    )
    op.execute("LOCK TABLE audit_logs IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""
        INSERT INTO operation_receipts (id, action, fingerprint, result_id)
        SELECT id, action, sha256(uuid_send(id) || convert_to(payload::text, 'UTF8')),
               CASE WHEN result_id ~ '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$'
                    THEN result_id::uuid END
        FROM (
            SELECT id, action,
                   CASE action
                       WHEN 'REPRESENTATIVE_ENDED' THEN coalesce(details->'reason', 'null'::jsonb)
                       WHEN 'REPRESENTATIVE_REJOINED' THEN 'null'::jsonb
                       WHEN 'TEACHER_REPLACEMENT_REQUESTED' THEN details->'new_teacher_id'
                       ELSE details->'payload'
                   END AS payload,
                   CASE action
                       WHEN 'REPRESENTATIVE_REJOINED' THEN details->>'new_membership_id'
                       WHEN 'TEACHER_REPLACEMENT_REQUESTED' THEN details->>'membership_id'
                   END AS result_id
            FROM audit_logs
            WHERE action IN ('ACTIVITY_DRAFT_CREATED', 'ACTIVITY_CORRECTION_STARTED',
                             'ACTIVITY_REPLACEMENT_STARTED', 'CLUB_RESUBMITTED',
                             'REPRESENTATIVE_ENDED', 'REPRESENTATIVE_REJOINED', 'TEACHER_REPLACEMENT_REQUESTED')
        ) AS legacy
    """)
    op.execute("""
        UPDATE audit_logs SET details = details - 'payload' - 'previous'
        WHERE action IN ('ACTIVITY_DRAFT_CREATED', 'ACTIVITY_CORRECTION_STARTED',
                         'ACTIVITY_REPLACEMENT_STARTED', 'CLUB_RESUBMITTED')
          AND (details ? 'payload' OR details ? 'previous')
    """)


def downgrade() -> None:
    op.execute("LOCK TABLE operation_receipts IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM operation_receipts) THEN
                RAISE EXCEPTION 'Downgrade would discard durable operation receipts';
            END IF;
        END $$
    """)
    op.drop_table("operation_receipts")
