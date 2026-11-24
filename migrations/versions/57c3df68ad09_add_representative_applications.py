"""Preserve applications and decisions for carried-forward clubs."""

import sqlalchemy as sa
from alembic import op


revision = "57c3df68ad09"
down_revision = "91b5d06e43c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "representative_applications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("club_id", sa.Uuid(), nullable=False),
        sa.Column("applicant_account_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING", "SELECTED", "REJECTED", "CHANGES_REQUESTED",
                name="ck_representative_application_status", native_enum=False, create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reviewed_by_account_id", sa.Uuid()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_operation_id", sa.Uuid()),
        sa.Column("review_comments", sa.String(5000)),
        sa.ForeignKeyConstraint(
            ["school_id", "club_id"], ["clubs.school_id", "clubs.id"],
            name="fk_representative_application_club_school",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "applicant_account_id"], ["accounts.school_id", "accounts.id"],
            name="fk_representative_application_applicant_school",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "reviewed_by_account_id"], ["accounts.school_id", "accounts.id"],
            name="fk_representative_application_reviewer_school",
        ),
        sa.UniqueConstraint("club_id", "applicant_account_id", name="uq_representative_application_club_applicant"),
        sa.UniqueConstraint("review_operation_id", name="uq_representative_application_review_operation"),
        sa.CheckConstraint(
            """(status = 'PENDING' AND reviewed_by_account_id IS NULL AND reviewed_at IS NULL
                AND review_operation_id IS NULL AND review_comments IS NULL)
            OR (status <> 'PENDING' AND reviewed_by_account_id IS NOT NULL AND reviewed_at IS NOT NULL
                AND reviewed_at >= submitted_at AND review_operation_id IS NOT NULL
                AND (status = 'SELECTED' OR coalesce(length(btrim(review_comments)), 0) > 0))""",
            name="ck_representative_application_review",
        ),
    )
    op.create_index(
        "uq_one_selected_representative_application_per_club", "representative_applications", ["club_id"],
        unique=True, postgresql_where=sa.text("status = 'SELECTED'"),
    )
    op.create_index(
        "ix_representative_applications_school_club", "representative_applications", ["school_id", "club_id", "id"],
    )
    op.create_index(
        "ix_representative_applications_school_applicant", "representative_applications",
        ["school_id", "applicant_account_id", "id"],
    )
    op.execute("""
        CREATE FUNCTION enforce_rollover_membership_school() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM clubs
                WHERE id = NEW.club_id AND rolled_over_from_club_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM accounts WHERE id = NEW.account_id AND school_id = clubs.school_id
                  )
            ) THEN
                RAISE EXCEPTION 'Carried-forward club and representative must belong to the same school'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_rollover_membership_same_school';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_rollover_membership_same_school
        BEFORE INSERT OR UPDATE OF club_id, account_id ON club_memberships
        FOR EACH ROW EXECUTE FUNCTION enforce_rollover_membership_school();
    """)


def downgrade() -> None:
    op.execute("LOCK TABLE representative_applications IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM representative_applications) THEN
                RAISE EXCEPTION 'Cannot downgrade after recording representative applications';
            END IF;
        END;
        $$;
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_rollover_membership_same_school ON club_memberships")
    op.execute("DROP FUNCTION IF EXISTS enforce_rollover_membership_school()")
    op.drop_table("representative_applications")
