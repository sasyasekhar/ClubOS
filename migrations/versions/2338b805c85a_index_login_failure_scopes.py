"""Support bounded login failure throttling across application processes."""

import sqlalchemy as sa
from alembic import op


revision = "2338b805c85a"
down_revision = "9c9b83ccaf31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for scope in ("client", "identifier"):
        op.add_column("login_history", sa.Column(f"{scope}_key", sa.String(64)))
        op.create_index(f"ix_login_failures_{scope}", "login_history", [f"{scope}_key", "occurred_at"],
                        postgresql_where=sa.text("outcome = 'FAILED'"))


def downgrade() -> None:
    for scope in ("client", "identifier"):
        op.drop_index(f"ix_login_failures_{scope}", table_name="login_history")
        op.drop_column("login_history", f"{scope}_key")
