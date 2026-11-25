"""Give a database an identity for its private storage pairing."""

import sqlalchemy as sa
from alembic import op


revision = "2df07cec79a1"
down_revision = "41c1fd217aed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_state",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("storage_identity", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("storage_environment", sa.String(20)),
        sa.Column("orphan_cursor", sa.String(500)),
        sa.CheckConstraint("id = 1", name="ck_maintenance_state_singleton"),
    )
    op.execute("INSERT INTO maintenance_state (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("maintenance_state")
