import uuid

from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Activity(Base):
    __tablename__ = "activities"

    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "club_id"],
            ["clubs.school_id", "clubs.id"],
            name="fk_activity_club_same_school",
        ),

        ForeignKeyConstraint(
            ["school_id", "created_by"],
            ["accounts.school_id", "accounts.id"],
            name="fk_activity_creator_same_school",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    school_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
    )

    club_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
    )

    created_by: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    