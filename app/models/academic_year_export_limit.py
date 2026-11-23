from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AcademicYearExportLimit(Base):
    __tablename__ = "academic_year_export_limits"

    school_id: Mapped[UUID] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"), primary_key=True
    )
    last_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
