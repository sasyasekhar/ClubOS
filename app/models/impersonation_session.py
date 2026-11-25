import uuid
from datetime import datetime

from sqlalchemy import (
    Index,
    String,
    Text,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
        ForeignKeyConstraint(
            ["auth_session_id", "admin_account_id"],
            ["auth_sessions.id", "auth_sessions.account_id"],
            name="fk_impersonation_auth_session_admin",
        ),
        CheckConstraint(
            "(auth_session_id IS NULL) = (school_id IS NULL) AND (status <> 'ACTIVE' OR auth_session_id IS NOT NULL)",
            name="ck_impersonation_parent_binding",
        ),
        CheckConstraint(
            "(auth_session_id IS NULL) = (expires_at IS NULL) "
            "AND (expires_at IS NULL OR expires_at <= started_at + interval '15 minutes')",
            name="ck_impersonation_expiration",
        ),
        CheckConstraint(
            """auth_session_id IS NULL OR (
                char_length(reason) <= 500 AND reason ~ '[^[:space:]]' AND (
                    (status = 'ACTIVE' AND ended_at IS NULL AND end_reason IS NULL)
                    OR (status <> 'ACTIVE' AND ended_at IS NOT NULL AND ended_at >= started_at AND end_reason IS NOT NULL)
                )
            )""",
            name="ck_impersonation_bound_lifecycle",
        ),
        Index(
            "uq_one_active_impersonation_per_admin", "admin_account_id", unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_impersonation_auth_session_started", "auth_session_id", "started_at", "id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
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

    # Historical pre-binding rows remain readable, but cannot grant authority.
    auth_session_id: Mapped[uuid.UUID | None]
    school_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("schools.id"))

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

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    end_reason: Mapped[str | None] = mapped_column(String(500))
