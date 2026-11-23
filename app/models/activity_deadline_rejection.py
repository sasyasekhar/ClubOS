from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


DEADLINE_EXPIRY_REASON = "Academic-year deadline expired"


class ActivityDeadlineRejection(Base):
    """System-action history independent of reviews performed by accounts."""

    __tablename__ = "activity_deadline_rejections"
    __table_args__ = (
        CheckConstraint(f"reason = '{DEADLINE_EXPIRY_REASON}'", name="ck_activity_deadline_rejection_reason"),
    )

    revision_id: Mapped[UUID] = mapped_column(ForeignKey("activity_revisions.id"), primary_key=True)
    closure_request_id: Mapped[UUID] = mapped_column(ForeignKey("academic_year_closure_requests.id"), index=True)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(60))
