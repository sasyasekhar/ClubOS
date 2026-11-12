import uuid

from sqlalchemy import (
    UUID,
    ForeignKey,
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

    __table_args__ = ()

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    club_id = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clubs.id"),
        nullable=False
    )

    admin_account_id = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("school_admins.account_id"),
        nullable=False
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
    