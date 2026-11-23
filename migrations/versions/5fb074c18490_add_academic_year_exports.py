"""Add export cooldowns and indexes for school-scoped reports."""

import sqlalchemy as sa
from alembic import op


revision = "5fb074c18490"
down_revision = "06fcae41e4a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "academic_year_export_limits",
        sa.Column(
            "school_id", sa.Uuid(), sa.ForeignKey("schools.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_clubs_school_academic_year", "clubs", ["school_id", "academic_year_id", "id"])
    op.create_index("ix_club_memberships_club_id", "club_memberships", ["club_id"])
    op.create_index("ix_activities_club_id", "activities", ["club_id"])
    op.create_index("ix_club_reviews_club_id", "club_reviews", ["club_id"])


def downgrade() -> None:
    op.drop_index("ix_club_reviews_club_id", table_name="club_reviews")
    op.drop_index("ix_activities_club_id", table_name="activities")
    op.drop_index("ix_club_memberships_club_id", table_name="club_memberships")
    op.drop_index("ix_clubs_school_academic_year", table_name="clubs")
    op.drop_table("academic_year_export_limits")
