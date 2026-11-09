import uuid


from sqlalchemy import (
    ForeignKey,
    ForeignKeyConstraint,
    CheckConstraint,
    UniqueConstraint,
    DateTime,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import AccountType


class SchoolAdmin(Base):
    __tablename__ = "school_admins"

    __table_args__ = (
        UniqueConstraint("account_id", name="uq_school_admin_account"),

        UniqueConstraint(
            "school_id",
            "account_id",
            name="uq_school_admin_school_account"
        ),

        CheckConstraint(
            "account_type = 'SCHOOL_ADMIN'",
            name="ck_school_admin_account_type"
        ),

        ForeignKeyConstraint(
            ["school_id", "account_id", "account_type"],
            [
                "accounts.school_id",
                "accounts.id",
                "accounts.account_type"
            ],
            name="fk_school_admin_same_school_admin_type"
        ),
    )

    school_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schools.id"),
        primary_key=True,
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    account_type: Mapped[AccountType] = mapped_column(
    default=AccountType.SCHOOL_ADMIN,
    nullable=False)
    