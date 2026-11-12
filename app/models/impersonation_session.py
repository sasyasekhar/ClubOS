import uuid

from sqlalchemy import (
    Index,
    String,
    Text,
    DateTime,
    ForeignKey,
    CheckConstraint,
    Enum as SAEnum,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base
from app.models.enums import ImpersonationStatus


class ImpersonationSession(Base):
    __tablename__ = "impersonation_sessions"

    __table_args__ = (
        CheckConstraint(
            "admin_account_id != target_account_id",
            name="ck_impersonation_different_accounts",
        ),
        UniqueConstraint(
            "id",
            "admin_account_id",
            name="uq_impersonation_session_id_admin",
        ),

        UniqueConstraint(
            "id",
            "admin_account_id",
            "target_account_id",
            name="uq_impersonation_session_id_admin_target",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    Index(
    "uq_one_active_impersonation_per_admin",
    "admin_account_id",
    unique=True,
    postgresql_where=text("status = 'ACTIVE'"),
    )

    admin_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id"),
        nullable=False,
        index=True,
    )

    target_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id"),
        nullable=False,
        index=True,
    )

    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    status: Mapped[ImpersonationStatus] = mapped_column(
        SAEnum(
            ImpersonationStatus,
            name="impersonation_status",
        ),
        default=ImpersonationStatus.ACTIVE,
        nullable=False,
    )

    started_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    ended_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
