"""Store closure deadlines and preserve system rejections as separate history."""

import sqlalchemy as sa
from alembic import op


revision = "92f0cb15a83e"
down_revision = "b7e43201fe21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    requests = "academic_year_closure_requests"
    op.add_column(requests, sa.Column("completion_deadline_at", sa.DateTime(timezone=True)))
    op.add_column(requests, sa.Column("finalized_at", sa.DateTime(timezone=True)))
    # Use elapsed hours so daylight-saving transitions do not change the deadline.
    op.execute("""
        UPDATE academic_year_closure_requests
        SET completion_deadline_at = requested_at + INTERVAL '168 hours'
    """)
    op.alter_column(requests, "completion_deadline_at", nullable=False)
    op.create_check_constraint(
        "ck_closure_request_completion_window", requests,
        "completion_deadline_at = requested_at + INTERVAL '168 hours'",
    )
    op.create_check_constraint(
        "ck_closure_request_finalized_after_deadline", requests,
        "finalized_at IS NULL OR finalized_at >= completion_deadline_at",
    )
    op.create_index(
        "ix_closure_request_unfinalized_deadline", requests, ["completion_deadline_at", "id"],
        postgresql_where=sa.text("status = 'PENDING' AND finalized_at IS NULL"),
    )

    op.create_table(
        "activity_deadline_rejections",
        sa.Column("revision_id", sa.Uuid(), sa.ForeignKey("activity_revisions.id"), primary_key=True),
        sa.Column(
            "closure_request_id", sa.Uuid(), sa.ForeignKey("academic_year_closure_requests.id"), nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(60), nullable=False),
        sa.CheckConstraint("reason = 'Academic-year deadline expired'", name="ck_activity_deadline_rejection_reason"),
    )
    op.create_index("ix_activity_deadline_rejections_closure_request_id", "activity_deadline_rejections", ["closure_request_id"])


def downgrade() -> None:
    requests = "academic_year_closure_requests"
    op.execute("LOCK TABLE academic_year_closure_requests, activity_deadline_rejections IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM activity_deadline_rejections)
                OR EXISTS (SELECT 1 FROM academic_year_closure_requests WHERE finalized_at IS NOT NULL)
            THEN
                RAISE EXCEPTION 'Cannot downgrade after recording completion-window finalization history';
            END IF;
        END $$;
    """)
    op.drop_table("activity_deadline_rejections")
    op.drop_index("ix_closure_request_unfinalized_deadline", table_name=requests)
    op.drop_constraint("ck_closure_request_finalized_after_deadline", requests, type_="check")
    op.drop_constraint("ck_closure_request_completion_window", requests, type_="check")
    op.drop_column(requests, "finalized_at")
    op.drop_column(requests, "completion_deadline_at")
