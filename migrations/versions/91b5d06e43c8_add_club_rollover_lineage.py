"""Record selective club rollover lineage and durable replays."""

import sqlalchemy as sa
from alembic import op


revision = "91b5d06e43c8"
down_revision = "e6f1451d28aa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clubs", sa.Column("rolled_over_from_club_id", sa.Uuid()))
    op.add_column("clubs", sa.Column("rollover_batch_id", sa.Uuid()))
    op.alter_column("clubs", "created_by", nullable=True)
    op.create_foreign_key(
        "fk_club_rollover_source_same_school", "clubs", "clubs",
        ["school_id", "rolled_over_from_club_id"], ["school_id", "id"],
    )
    op.create_unique_constraint(
        "uq_clubs_rollover_source_year", "clubs", ["school_id", "academic_year_id", "rolled_over_from_club_id"],
    )
    op.create_check_constraint(
        "ck_club_creator_or_rollover", "clubs", "created_by IS NOT NULL OR rolled_over_from_club_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_club_rollover_batch", "clubs", "(rolled_over_from_club_id IS NULL) = (rollover_batch_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_club_rollover_distinct_source", "clubs", "rolled_over_from_club_id IS NULL OR rolled_over_from_club_id <> id",
    )
    op.create_index(
        "ix_clubs_rollover_batch_school", "clubs", ["school_id", "rollover_batch_id"],
        postgresql_where=sa.text("rollover_batch_id IS NOT NULL"),
    )
    op.execute("""
        CREATE FUNCTION enforce_club_rollover_lineage() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.rolled_over_from_club_id IS DISTINCT FROM OLD.rolled_over_from_club_id
                OR NEW.rollover_batch_id IS DISTINCT FROM OLD.rollover_batch_id
                OR (OLD.rolled_over_from_club_id IS NOT NULL AND (
                    NEW.school_id IS DISTINCT FROM OLD.school_id
                    OR NEW.academic_year_id IS DISTINCT FROM OLD.academic_year_id
                )) THEN
                RAISE EXCEPTION 'Club rollover lineage is immutable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER ck_club_rollover_lineage_immutable
        BEFORE UPDATE OF school_id, academic_year_id, rolled_over_from_club_id, rollover_batch_id ON clubs
        FOR EACH ROW EXECUTE FUNCTION enforce_club_rollover_lineage();
    """)


def downgrade() -> None:
    op.execute("LOCK TABLE clubs IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM clubs WHERE rolled_over_from_club_id IS NOT NULL) THEN
                RAISE EXCEPTION 'Cannot downgrade after recording club rollover history';
            END IF;
        END;
        $$;
    """)
    op.execute("DROP TRIGGER ck_club_rollover_lineage_immutable ON clubs")
    op.execute("DROP FUNCTION enforce_club_rollover_lineage()")
    op.drop_index("ix_clubs_rollover_batch_school", table_name="clubs")
    op.drop_constraint("ck_club_rollover_distinct_source", "clubs", type_="check")
    op.drop_constraint("ck_club_rollover_batch", "clubs", type_="check")
    op.drop_constraint("ck_club_creator_or_rollover", "clubs", type_="check")
    op.drop_constraint("uq_clubs_rollover_source_year", "clubs", type_="unique")
    op.drop_constraint("fk_club_rollover_source_same_school", "clubs", type_="foreignkey")
    op.alter_column("clubs", "created_by", nullable=False)
    op.drop_column("clubs", "rollover_batch_id")
    op.drop_column("clubs", "rolled_over_from_club_id")
