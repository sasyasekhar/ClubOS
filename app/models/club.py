import uuid

from sqlalchemy import (
    UUID,
    String,
    Text,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
    Enum as SAEnum,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.models.enums import AccountType
from app.models.base import Base
from app.models.enums import ClubStatus


class Club(Base):
    __tablename__ = "clubs"

    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "academic_year_id"],
            ["academic_years.school_id", "academic_years.id"],
            name="fk_club_academic_year_same_school",
        ),

        ForeignKeyConstraint(
            ["school_id", "created_by"],
            ["accounts.school_id", "accounts.id"],
            name="fk_club_creator_same_school",
        ),

        UniqueConstraint(
            "school_id",
            "id",
            name="uq_clubs_school_id_id",
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

    academic_year_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_years.id"),
        nullable=False,
        
    )

    created_by = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id"),
        nullable=False
    )

    name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    category: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    logo_key: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    status: Mapped[ClubStatus] = mapped_column(
        SAEnum(ClubStatus, name="club_status"),
        default=ClubStatus.PENDING,
        nullable=False,
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
   