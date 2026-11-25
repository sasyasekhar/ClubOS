from uuid import UUID

from sqlalchemy import LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OperationReceipt(Base):
    __tablename__ = "operation_receipts"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(100))
    fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary)
    result_id: Mapped[UUID | None]
