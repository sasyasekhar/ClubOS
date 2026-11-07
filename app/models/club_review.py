import uuid

from sqlalchemy import (
    String,
    Text,
    DateTime,
    ForeignKeyConstraint,
    Enum as SAEnum,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import ClubReviewDecision


class ClubReview(Base):
    __tablename__ = "club_reviews"

    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "club_id"],
            ["clubs.school_id", "clubs.id"],
            name="fk_club_review_same_school",
        ),

        ForeignKeyConstraint(
            ["school_id", "admin_account_id"],
            ["school_admins.school_id", "school_admins.account_id"],
            name="fk_club_review_school_admin",
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

    admin_account_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
    )

    decision: Mapped[ClubReviewDecision] = mapped_column(
        SAEnum(ClubReviewDecision, name="club_review_decision"),
        nullable=False,
    )

    comments: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    reviewed_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    