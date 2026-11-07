import uuid

from sqlalchemy import (
    String,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
    CheckConstraint,
    Index,
    Enum as SAEnum,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import AcademicYearStatus


class AcademicYear(Base):
    __tablename__ = "academic_years"

    __table_args__ = (
        UniqueConstraint(
            "school_id",
            "name",
            name="uq_academic_year_school_name",
        ),

        UniqueConstraint(
            "school_id",
            "id",
            name="uq_academic_year_school_id",
        ),

        CheckConstraint(
            "end_date > start_date",
            name="ck_academic_year_dates",
        ),

        Index(
            "uq_one_active_academic_year_per_school",
            "school_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    school_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schools.id"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    start_date: Mapped[Date] = mapped_column(
        Date,
        nullable=False,
    )

    end_date: Mapped[Date] = mapped_column(
        Date,
        nullable=False,
    )

    status: Mapped[AcademicYearStatus] = mapped_column(
        SAEnum(AcademicYearStatus, name="academic_year_status"),
        default=AcademicYearStatus.ACTIVE,
        nullable=False,
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )