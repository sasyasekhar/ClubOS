import uuid

from sqlalchemy import (
    String,
    Text,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    __table_args__ = (
        ForeignKeyConstraint(
            ["impersonation_session_id", "actor_account_id"],
            [
                "impersonation_sessions.id",
                "impersonation_sessions.admin_account_id",
            ],
            name="fk_audit_impersonation_actor",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    actor_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id"),
        nullable=False,
        index=True,
    )

    represented_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id"),
        nullable=True,
    )

    impersonation_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("impersonation_sessions.id"),
        nullable=True,
    )

    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    entity_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    entity_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    details: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    occurred_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
