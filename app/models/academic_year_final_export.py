from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AcademicYearFinalExport(Base):
    """Durable evidence of a completed export from a closed-year snapshot."""

    __tablename__ = "academic_year_final_exports"
    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "academic_year_id"], ["academic_years.school_id", "academic_years.id"],
            name="fk_year_final_export_same_school", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["school_id", "exported_by_account_id"], ["accounts.school_id", "accounts.id"],
            name="fk_year_final_export_admin_same_school",
        ),
        ForeignKeyConstraint(
            ["school_id", "archived_by_account_id"], ["accounts.school_id", "accounts.id"],
            name="fk_year_archive_admin_same_school",
        ),
        UniqueConstraint("export_id", name="uq_year_final_export_id"),
        UniqueConstraint("school_id", "archive_idempotency_key", name="uq_year_archive_school_key"),
        CheckConstraint(
            """(archived_at IS NULL AND archived_by_account_id IS NULL AND archive_idempotency_key IS NULL)
            OR (archived_at >= generated_at AND archived_at IS NOT NULL
                AND archived_by_account_id IS NOT NULL AND archive_idempotency_key IS NOT NULL)""",
            name="ck_year_archive_metadata",
        ),
    )

    academic_year_id: Mapped[UUID] = mapped_column(primary_key=True)
    school_id: Mapped[UUID]
    export_id: Mapped[UUID]
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exported_by_account_id: Mapped[UUID]
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by_account_id: Mapped[UUID | None]
    archive_idempotency_key: Mapped[UUID | None]
