from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RepresentativeApplicationStatus(str, Enum):
    PENDING = "PENDING"
    SELECTED = "SELECTED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class RepresentativeApplication(Base):
    __tablename__ = "representative_applications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "club_id"], ["clubs.school_id", "clubs.id"],
            name="fk_representative_application_club_school",
        ),
        ForeignKeyConstraint(
            ["school_id", "applicant_account_id"], ["accounts.school_id", "accounts.id"],
            name="fk_representative_application_applicant_school",
        ),
        ForeignKeyConstraint(
            ["school_id", "reviewed_by_account_id"], ["accounts.school_id", "accounts.id"],
            name="fk_representative_application_reviewer_school",
        ),
        UniqueConstraint("club_id", "applicant_account_id", name="uq_representative_application_club_applicant"),
        UniqueConstraint("review_operation_id", name="uq_representative_application_review_operation"),
        CheckConstraint(
            """(status = 'PENDING' AND reviewed_by_account_id IS NULL AND reviewed_at IS NULL
                AND review_operation_id IS NULL AND review_comments IS NULL)
            OR (status <> 'PENDING' AND reviewed_by_account_id IS NOT NULL AND reviewed_at IS NOT NULL
                AND reviewed_at >= submitted_at AND review_operation_id IS NOT NULL
                AND (status = 'SELECTED' OR coalesce(length(btrim(review_comments)), 0) > 0))""",
            name="ck_representative_application_review",
        ),
        Index(
            "uq_one_selected_representative_application_per_club", "club_id", unique=True,
            postgresql_where=text("status = 'SELECTED'"),
        ),
        Index("ix_representative_applications_school_club", "school_id", "club_id", "id"),
        Index("ix_representative_applications_school_applicant", "school_id", "applicant_account_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    school_id: Mapped[UUID]
    club_id: Mapped[UUID]
    applicant_account_id: Mapped[UUID]
    status: Mapped[RepresentativeApplicationStatus] = mapped_column(
        SAEnum(
            RepresentativeApplicationStatus,
            name="ck_representative_application_status", native_enum=False, create_constraint=True,
        ),
        default=RepresentativeApplicationStatus.PENDING,
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_by_account_id: Mapped[UUID | None]
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_operation_id: Mapped[UUID | None]
    review_comments: Mapped[str | None] = mapped_column(String(5000))
