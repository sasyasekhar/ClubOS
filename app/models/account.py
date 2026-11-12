import uuid

from sqlalchemy import (
    String,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    CheckConstraint,
    Index,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import AccountType


class Account(Base):
    __tablename__ = "accounts"

    __table_args__ = (
        UniqueConstraint(
             "school_id", 
            "id",
            name="uq_accounts_school_id",
        ),

        CheckConstraint(
            """
            (
                account_type = 'PLATFORM_OWNER'
                AND school_id IS NULL
            )
            OR
            (
                account_type != 'PLATFORM_OWNER'
                AND school_id IS NOT NULL
            )
            """,
            name="ck_account_school_requirement",
        ),

        Index(
            "uq_accounts_username_lower",
            func.lower(text("username")),
            unique=True,
        ),

        Index(
            "uq_accounts_email_lower",
            func.lower(text("email")),
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    school_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("schools.id"),
        nullable=True,
    )

    account_type: Mapped[AccountType] = mapped_column(
        nullable=False,
    )

    full_name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )

    username: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
    )

    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    