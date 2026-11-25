import uuid
from datetime import datetime

from sqlalchemy import (
    String,
    DateTime,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Enum as SAEnum,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import DeletionStatus


class DeletedRecord(Base):
    __tablename__ = "deleted_records"
    __table_args__ = (
        ForeignKeyConstraint(["school_id", "deleted_by"], ["accounts.school_id", "accounts.id"],
                             name="fk_recovery_deleting_actor_school"),
        ForeignKeyConstraint(["school_id", "restored_by"], ["accounts.school_id", "accounts.id"],
                             name="fk_recovery_restoring_actor_school"),
        CheckConstraint(
            """school_id IS NULL OR (entity_type = 'ACTIVITY_DRAFT'
            AND expires_at = deleted_at + interval '720 hours' AND (
                (status = 'PENDING' AND revision_id IS NOT NULL AND entity_id = revision_id::text AND restored_by IS NULL
                    AND restored_at IS NULL AND restore_key IS NULL)
                OR (status = 'RESTORED' AND revision_id IS NULL AND restored_by IS NOT NULL
                    AND restored_at >= deleted_at AND restored_at < expires_at AND restore_key IS NOT NULL)
                OR (status = 'PERMANENTLY_DELETED' AND revision_id IS NULL AND restored_by IS NULL
                    AND restored_at IS NULL AND restore_key IS NULL)
            ))""", name="ck_owned_draft_recovery",
        ),
        Index("ix_deleted_records_pending_expiry", "expires_at", "id",
              postgresql_where=text("status = 'PENDING' AND school_id IS NOT NULL")),
        Index("uq_pending_draft_recovery", "revision_id", unique=True,
              postgresql_where=text("status = 'PENDING' AND school_id IS NOT NULL")),
    )

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

    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    restored_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    school_id: Mapped[uuid.UUID | None]
    revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("activity_revisions.id"))
    restored_by: Mapped[uuid.UUID | None]
    restore_key: Mapped[uuid.UUID | None]
