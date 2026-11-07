import uuid

from sqlalchemy import (
    String,
    Text,
    Date,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    CheckConstraint,
    Index,
    Enum as SAEnum,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import ActivityRevisionStatus


class ActivityRevision(Base):
    __tablename__ = "activity_revisions"

    __table_args__ = (
        UniqueConstraint(
            "activity_id",
            "revision_number",
            name="uq_activity_revision_number",
        ),

        CheckConstraint(
            "revision_number > 0",
            name="ck_revision_number_positive",
        ),

        CheckConstraint(
            "is_published = FALSE OR status = 'APPROVED'",
            name="ck_published_revision_approved",
        ),

        Index(
            "uq_one_published_revision_per_activity",
            "activity_id",
            unique=True,
            postgresql_where=text("is_published = TRUE"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    activity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("activities.id"),
        nullable=False,
    )

    revision_number: Mapped[int] = mapped_column(
        nullable=False,
    )

    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    activity_date: Mapped[Date] = mapped_column(
        Date,
        nullable=False,
    )

    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    category: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    status: Mapped[ActivityRevisionStatus] = mapped_column(
        SAEnum(
            ActivityRevisionStatus,
            name="activity_revision_status",
        ),
        default=ActivityRevisionStatus.DRAFT,
        nullable=False,
    )

    is_published: Mapped[bool] = mapped_column(
        default=False,
        nullable=False,
    )

    submitted_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    