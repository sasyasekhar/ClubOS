import uuid

from sqlalchemy import (
    String,
    DateTime,
    ForeignKey,
    Enum as SAEnum,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import DeletionStatus


class DeletedRecord(Base):
    __tablename__ = "deleted_records"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    entity_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    entity_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    deleted_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id"),
        nullable=False,
    )

    status: Mapped[DeletionStatus] = mapped_column(
        SAEnum(DeletionStatus, name="deletion_status"),
        default=DeletionStatus.PENDING,
        nullable=False,
    )

    deleted_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    expires_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    restored_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )