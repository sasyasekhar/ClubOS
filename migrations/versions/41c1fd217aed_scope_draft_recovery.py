"""Add school-owned recovery records for retained drafts."""

import sqlalchemy as sa
from alembic import op


revision = "41c1fd217aed"
down_revision = "d974512f234a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "deleted_records"
    for name in ("school_id", "revision_id", "restored_by", "restore_key"):
        op.add_column(table, sa.Column(name, sa.Uuid()))
    op.create_foreign_key("deleted_records_revision_id_fkey", table, "activity_revisions", ["revision_id"], ["id"])
    op.create_foreign_key("fk_recovery_deleting_actor_school", table, "accounts",
                          ["school_id", "deleted_by"], ["school_id", "id"])
    op.create_foreign_key("fk_recovery_restoring_actor_school", table, "accounts",
                          ["school_id", "restored_by"], ["school_id", "id"])
    op.create_check_constraint("ck_owned_draft_recovery", table, """
        school_id IS NULL OR (entity_type = 'ACTIVITY_DRAFT'
        AND expires_at = deleted_at + interval '720 hours' AND (
            (status = 'PENDING' AND revision_id IS NOT NULL AND entity_id = revision_id::text AND restored_by IS NULL
                AND restored_at IS NULL AND restore_key IS NULL)
            OR (status = 'RESTORED' AND revision_id IS NULL AND restored_by IS NOT NULL
                AND restored_at >= deleted_at AND restored_at < expires_at AND restore_key IS NOT NULL)
            OR (status = 'PERMANENTLY_DELETED' AND revision_id IS NULL AND restored_by IS NULL
                AND restored_at IS NULL AND restore_key IS NULL)
        ))
    """)
    pending = sa.text("status = 'PENDING' AND school_id IS NOT NULL")
    op.create_index("ix_deleted_records_pending_expiry", table, ["expires_at", "id"], postgresql_where=pending)
    op.create_index("uq_pending_draft_recovery", table, ["revision_id"], unique=True, postgresql_where=pending)


def downgrade() -> None:
    table = "deleted_records"
    op.execute("LOCK TABLE deleted_records IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM deleted_records WHERE school_id IS NOT NULL) THEN
                RAISE EXCEPTION 'Downgrade would discard school-owned recovery data';
            END IF;
        END $$
    """)
    for name in ("uq_pending_draft_recovery", "ix_deleted_records_pending_expiry"):
        op.drop_index(name, table_name=table)
    op.drop_constraint("ck_owned_draft_recovery", table, type_="check")
    for name in ("deleted_records_revision_id_fkey", "fk_recovery_deleting_actor_school", "fk_recovery_restoring_actor_school"):
        op.drop_constraint(name, table, type_="foreignkey")
    for name in ("restore_key", "restored_by", "revision_id", "school_id"):
        op.drop_column(table, name)
