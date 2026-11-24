"""Persist final export evidence and enforce forward academic-year transitions."""

import sqlalchemy as sa
from alembic import op


revision = "e6f1451d28aa"
down_revision = "92f0cb15a83e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "academic_year_final_exports",
        sa.Column("academic_year_id", sa.Uuid(), primary_key=True),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("export_id", sa.Uuid(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exported_by_account_id", sa.Uuid(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("archived_by_account_id", sa.Uuid()),
        sa.Column("archive_idempotency_key", sa.Uuid()),
        sa.ForeignKeyConstraint(
            ["school_id", "academic_year_id"], ["academic_years.school_id", "academic_years.id"],
            name="fk_year_final_export_same_school", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "exported_by_account_id"], ["accounts.school_id", "accounts.id"],
            name="fk_year_final_export_admin_same_school",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "archived_by_account_id"], ["accounts.school_id", "accounts.id"],
            name="fk_year_archive_admin_same_school",
        ),
        sa.UniqueConstraint("export_id", name="uq_year_final_export_id"),
        sa.UniqueConstraint("school_id", "archive_idempotency_key", name="uq_year_archive_school_key"),
        sa.CheckConstraint(
            """(archived_at IS NULL AND archived_by_account_id IS NULL AND archive_idempotency_key IS NULL)
            OR (archived_at >= generated_at AND archived_at IS NOT NULL
                AND archived_by_account_id IS NOT NULL AND archive_idempotency_key IS NOT NULL)""",
            name="ck_year_archive_metadata",
        ),
    )
    op.execute("""
        CREATE FUNCTION enforce_academic_year_status_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status = 'ACTIVE' AND NEW.status = 'CLOSED' THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'CLOSED' AND NEW.status = 'ARCHIVED'
                AND EXISTS (
                    SELECT 1 FROM academic_year_final_exports
                    WHERE academic_year_id = OLD.id AND school_id = OLD.school_id AND archived_at IS NOT NULL
                )
            THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'Invalid academic-year status transition'
                USING ERRCODE = '23514', CONSTRAINT = 'ck_academic_year_status_transition';
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_academic_year_status_transition
        BEFORE UPDATE OF status ON academic_years FOR EACH ROW
        WHEN (OLD.status IS DISTINCT FROM NEW.status)
        EXECUTE FUNCTION enforce_academic_year_status_transition();
    """)


def downgrade() -> None:
    op.execute("LOCK TABLE academic_years, academic_year_final_exports IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM academic_year_final_exports) THEN
                RAISE EXCEPTION 'Cannot downgrade after recording final export or archival evidence';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER trg_academic_year_status_transition ON academic_years")
    op.execute("DROP FUNCTION enforce_academic_year_status_transition()")
    op.drop_table("academic_year_final_exports")
