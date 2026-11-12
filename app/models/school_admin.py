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

        ForeignKeyConstraint(
            ["school_id", "account_id"],
            [ "accounts.school_id", "accounts.id"], name="fk_school_admin_same_school"
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

    