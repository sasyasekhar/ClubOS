"""Keep the original impersonation cutoff across parent renewals."""

import sqlalchemy as sa
from alembic import op


revision = "fe7b134d822d"
down_revision = "c4d15a9e7b62"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("impersonation_sessions", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.execute("""
        UPDATE impersonation_sessions AS impersonation
        SET expires_at = least(impersonation.started_at + interval '15 minutes', parent.expires_at)
        FROM auth_sessions AS parent
        WHERE parent.id = impersonation.auth_session_id
    """)
    op.create_check_constraint(
        "ck_impersonation_expiration", "impersonation_sessions",
        "(auth_session_id IS NULL) = (expires_at IS NULL) "
        "AND (expires_at IS NULL OR expires_at <= started_at + interval '15 minutes')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_impersonation_expiration", "impersonation_sessions", type_="check")
    op.drop_column("impersonation_sessions", "expires_at")
