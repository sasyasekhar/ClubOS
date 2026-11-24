"""Bind administrator impersonation to its authenticated tab."""

import sqlalchemy as sa
from alembic import op


revision = "c4d15a9e7b62"
down_revision = "57c3df68ad09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_auth_session_id_account", "auth_sessions", ["id", "account_id"])
    op.add_column("impersonation_sessions", sa.Column("auth_session_id", sa.Uuid()))
    op.add_column("impersonation_sessions", sa.Column("school_id", sa.Uuid(), sa.ForeignKey("schools.id")))
    op.add_column("impersonation_sessions", sa.Column("end_reason", sa.String(500)))
    op.create_foreign_key(
        "fk_impersonation_auth_session_admin", "impersonation_sessions", "auth_sessions",
        ["auth_session_id", "admin_account_id"], ["id", "account_id"],
    )
    # Pre-binding rows have no authenticated tab to validate. Keep the records
    # and all review references, and audit the invalidation of any active rows.
    op.execute("""
        WITH invalidated AS (
            UPDATE impersonation_sessions
            SET status = 'EXPIRED', ended_at = greatest(clock_timestamp(), started_at),
                end_reason = 'UNBOUND_LEGACY_SESSION'
            WHERE status = 'ACTIVE'
            RETURNING id, admin_account_id, target_account_id, ended_at
        )
        INSERT INTO audit_logs (
            id, actor_account_id, represented_account_id, impersonation_session_id,
            action, entity_type, entity_id, details, occurred_at
        )
        SELECT gen_random_uuid(), admin_account_id, target_account_id, id,
            'IMPERSONATION_INVALIDATED', 'impersonation_session', id::text,
            jsonb_build_object('reason', 'UNBOUND_LEGACY_SESSION'), ended_at
        FROM invalidated
    """)
    op.create_check_constraint(
        "ck_impersonation_parent_binding", "impersonation_sessions",
        "(auth_session_id IS NULL) = (school_id IS NULL) AND (status <> 'ACTIVE' OR auth_session_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_impersonation_bound_lifecycle", "impersonation_sessions",
        """auth_session_id IS NULL OR (
            char_length(reason) <= 500 AND reason ~ '[^[:space:]]' AND (
                (status = 'ACTIVE' AND ended_at IS NULL AND end_reason IS NULL)
                OR (status <> 'ACTIVE' AND ended_at IS NOT NULL AND ended_at >= started_at AND end_reason IS NOT NULL)
            )
        )""",
    )
    op.create_index(
        "uq_one_active_impersonation_per_admin", "impersonation_sessions", ["admin_account_id"],
        unique=True, postgresql_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_index(
        "ix_impersonation_auth_session_started", "impersonation_sessions", ["auth_session_id", "started_at", "id"],
    )


def downgrade() -> None:
    op.execute("LOCK TABLE auth_sessions, impersonation_sessions IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM impersonation_sessions WHERE auth_session_id IS NOT NULL) THEN
                RAISE EXCEPTION 'Cannot downgrade after recording tab-bound impersonation sessions';
            END IF;
        END;
        $$;
    """)
    op.drop_index("ix_impersonation_auth_session_started", table_name="impersonation_sessions")
    op.drop_index("uq_one_active_impersonation_per_admin", table_name="impersonation_sessions")
    op.drop_constraint("ck_impersonation_bound_lifecycle", "impersonation_sessions", type_="check")
    op.drop_constraint("ck_impersonation_parent_binding", "impersonation_sessions", type_="check")
    op.drop_constraint("fk_impersonation_auth_session_admin", "impersonation_sessions", type_="foreignkey")
    op.drop_column("impersonation_sessions", "end_reason")
    op.drop_column("impersonation_sessions", "school_id")
    op.drop_column("impersonation_sessions", "auth_session_id")
    op.drop_constraint("uq_auth_session_id_account", "auth_sessions", type_="unique")
