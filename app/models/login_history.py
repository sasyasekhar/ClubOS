import uuid

from sqlalchemy import (
    String,
    DateTime,
    ForeignKey,
    Index,
    Enum as SAEnum,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import LoginOutcome


class LoginHistory(Base):
    __tablename__ = "login_history"
    __table_args__ = (
        Index("ix_login_failures_client", "client_key", "occurred_at", postgresql_where=text("outcome = 'FAILED'")),
        Index("ix_login_failures_identifier", "identifier_key", "occurred_at", postgresql_where=text("outcome = 'FAILED'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    attempted_identifier: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    client_key: Mapped[str | None] = mapped_column(String(64))
    identifier_key: Mapped[str | None] = mapped_column(String(64))

    outcome: Mapped[LoginOutcome] = mapped_column(
        SAEnum(LoginOutcome, name="login_outcome"),
        nullable=False,
    )

    occurred_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
