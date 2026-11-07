import uuid

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKeyConstraint,
    UniqueConstraint,
    Index,
    Enum as SAEnum,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import ClubRole, MembershipStatus


class ClubMembership(Base):
    __tablename__ = "club_memberships"

    __table_args__ = (
        ForeignKeyConstraint(
            ["school_id", "club_id"],
            ["clubs.school_id", "clubs.id"],
            name="fk_membership_club_same_school",
        ),

        ForeignKeyConstraint(
            ["school_id", "account_id"],
            ["accounts.school_id", "accounts.id"],
            name="fk_membership_account_same_school",
        ),

        UniqueConstraint(
            "club_id",
            "account_id",
            "role",
            name="uq_club_account_role",
        ),

        Index(
            "uq_one_current_representative_per_role",
            "club_id",
            "role",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'ACTIVE')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    school_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
    )

    club_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
    )

    role: Mapped[ClubRole] = mapped_column(
        SAEnum(ClubRole, name="club_role"),
        nullable=False,
    )

    status: Mapped[MembershipStatus] = mapped_column(
        SAEnum(MembershipStatus, name="membership_status"),
        default=MembershipStatus.PENDING,
        nullable=False,
    )

    start_date: Mapped[Date] = mapped_column(
        Date,
        nullable=False,
    )

    end_date: Mapped[Date | None] = mapped_column(
        Date,
        nullable=True,
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    