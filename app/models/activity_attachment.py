import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ActivityAttachment(Base):
    __tablename__ = "activity_attachments"
    __table_args__ = (
        UniqueConstraint(
            "revision_id", "idempotency_key", name="uq_attachment_revision_idempotency"
        ),
        CheckConstraint(
            """(
                idempotency_key IS NULL AND size_bytes IS NULL AND content_sha256 IS NULL
            ) OR (
                idempotency_key IS NOT NULL
                AND size_bytes IS NOT NULL AND size_bytes BETWEEN 1 AND 10485760
                AND content_sha256 IS NOT NULL AND content_sha256 ~ '^[0-9a-f]{64}$'
            )""",
            name="ck_attachment_upload_metadata",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("activity_revisions.id"),
        nullable=False,
        index=True,
    )

    file_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    storage_key: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        index=True,
    )

    file_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    idempotency_key: Mapped[uuid.UUID | None]
    size_bytes: Mapped[int | None]
    content_sha256: Mapped[str | None] = mapped_column(String(64))

    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
