from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint, DateTime, Enum, ForeignKeyConstraint, Index, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import ClosureRequestStatus


class AcademicYearClosureRequest(Base):
    __tablename__ = "academic_year_closure_requests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "academic_year_id"], ["academic_years.school_id", "academic_years.id"],
            name="fk_closure_request_year_same_school",
        ),
        ForeignKeyConstraint(
            ["school_id", "teacher_account_id"], ["accounts.school_id", "accounts.id"],
            name="fk_closure_request_teacher_same_school",
        ),
        ForeignKeyConstraint(
            ["school_id", "confirmed_by_account_id"], ["accounts.school_id", "accounts.id"],
            name="fk_closure_request_admin_same_school",
        ),
        UniqueConstraint("school_id", "idempotency_key", name="uq_closure_request_school_key"),
        CheckConstraint(
            """(status = 'CONFIRMED' AND confirmed_by_account_id IS NOT NULL AND confirmed_at IS NOT NULL)
            OR (status <> 'CONFIRMED' AND confirmed_by_account_id IS NULL AND confirmed_at IS NULL)""",
            name="ck_closure_request_confirmation",
        ),
        CheckConstraint(
            "completion_deadline_at = requested_at + INTERVAL '168 hours'",
            name="ck_closure_request_completion_window",
        ),
        CheckConstraint(
            "finalized_at IS NULL OR finalized_at >= completion_deadline_at",
            name="ck_closure_request_finalized_after_deadline",
        ),
        Index(
            "uq_current_closure_request_year", "school_id", "academic_year_id", unique=True,
            postgresql_where=text("status IN ('PENDING', 'CONFIRMED')"),
        ),
        Index(
            "ix_closure_request_unfinalized_deadline", "completion_deadline_at", "id",
            postgresql_where=text("status = 'PENDING' AND finalized_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    school_id: Mapped[UUID]
    academic_year_id: Mapped[UUID]
    teacher_account_id: Mapped[UUID]
    designation_id: Mapped[UUID]
    idempotency_key: Mapped[UUID]
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completion_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[ClosureRequestStatus] = mapped_column(Enum(
        ClosureRequestStatus, native_enum=False, length=16, create_constraint=True,
        name="ck_closure_request_status",
    ))
    confirmed_by_account_id: Mapped[UUID | None]
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
