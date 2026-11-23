"""Add school-wide closure teacher designations and academic-year closure requests."""

import sqlalchemy as sa
from alembic import op


revision = "b7e43201fe21"
down_revision = "5fb074c18490"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("schools", sa.Column("closure_teacher_account_id", sa.Uuid()))
    op.add_column("schools", sa.Column("closure_teacher_designation_id", sa.Uuid()))
    op.create_check_constraint(
        "ck_school_closure_teacher_designation", "schools",
        "(closure_teacher_account_id IS NULL) = (closure_teacher_designation_id IS NULL)",
    )
    op.create_foreign_key(
        "fk_school_closure_teacher_same_school", "schools", "accounts",
        ["id", "closure_teacher_account_id"], ["school_id", "id"],
    )

    op.create_table(
        "academic_year_closure_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column("academic_year_id", sa.Uuid(), nullable=False),
        sa.Column("teacher_account_id", sa.Uuid(), nullable=False),
        sa.Column("designation_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.Uuid(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("status", sa.Enum(
            "PENDING", "CONFIRMED", name="ck_closure_request_status", native_enum=False,
            create_constraint=True, length=16,
        ), nullable=False),
        sa.Column("confirmed_by_account_id", sa.Uuid()),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            """(status = 'CONFIRMED' AND confirmed_by_account_id IS NOT NULL AND confirmed_at IS NOT NULL)
            OR (status <> 'CONFIRMED' AND confirmed_by_account_id IS NULL AND confirmed_at IS NULL)""",
            name="ck_closure_request_confirmation",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "academic_year_id"], ["academic_years.school_id", "academic_years.id"],
            name="fk_closure_request_year_same_school",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "teacher_account_id"], ["accounts.school_id", "accounts.id"],
            name="fk_closure_request_teacher_same_school",
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "confirmed_by_account_id"], ["accounts.school_id", "accounts.id"],
            name="fk_closure_request_admin_same_school",
        ),
        sa.UniqueConstraint("school_id", "idempotency_key", name="uq_closure_request_school_key"),
    )
    op.create_index(
        "uq_current_closure_request_year", "academic_year_closure_requests",
        ["school_id", "academic_year_id"], unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'CONFIRMED')"),
    )


def downgrade() -> None:
    op.drop_table("academic_year_closure_requests")
    op.drop_constraint("fk_school_closure_teacher_same_school", "schools", type_="foreignkey")
    op.drop_constraint("ck_school_closure_teacher_designation", "schools", type_="check")
    op.drop_column("schools", "closure_teacher_designation_id")
    op.drop_column("schools", "closure_teacher_account_id")
