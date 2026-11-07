import uuid

from sqlalchemy import (
    String,
    Text,
    DateTime,
    ForeignKey,
    CheckConstraint,
    Enum as SAEnum,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import ActivityReviewDecision, ReviewMode


class ActivityReview(Base):
    __tablename__ = "activity_reviews"

    __table_args__ = (
        CheckConstraint(
            """
            (
                review_mode = 'DIRECT'
                AND represented_account_id IS NULL
            )
            OR
            (
                review_mode = 'IMPERSONATION'
                AND represented_account_id IS NOT NULL
            )
            """,
            name="ck_activity_review_impersonation",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("activity_revisions.id"),
        nullable=False,
        index=True,
    )

    # The actual person who performed the review.
    actor_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id"),
        nullable=False,
    )

    # The account whose interface was being represented.
    # NULL for a normal, direct review.
    represented_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id"),
        nullable=True,
    )

    review_mode: Mapped[ReviewMode] = mapped_column(
        SAEnum(ReviewMode, name="review_mode"),
        nullable=False,
    )

    decision: Mapped[ActivityReviewDecision] = mapped_column(
        SAEnum(
            ActivityReviewDecision,
            name="activity_review_decision",
        ),
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
    