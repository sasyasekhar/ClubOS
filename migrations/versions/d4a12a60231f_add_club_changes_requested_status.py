"""add club changes requested status

Revision ID: d4a12a60231f
Revises: 0ccd157b3e93
Create Date: 2026-11-18 19:54:53.158703

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a12a60231f'
down_revision: Union[str, Sequence[str], None] = '0ccd157b3e93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE club_status "
        "ADD VALUE IF NOT EXISTS 'CHANGES_REQUESTED'"
    )


def downgrade() -> None:
    op.execute("""
        UPDATE clubs SET status = 'PENDING'
        WHERE status = 'CHANGES_REQUESTED'
    """)
    op.execute("ALTER TYPE club_status RENAME TO club_status_old")
    op.execute("""
        CREATE TYPE club_status AS ENUM (
            'PENDING', 'ACTIVE', 'REJECTED', 'ARCHIVED'
        )
    """)
    op.execute("""
        ALTER TABLE clubs
        ALTER COLUMN status TYPE club_status
        USING status::text::club_status
    """)
    op.execute("DROP TYPE club_status_old")
