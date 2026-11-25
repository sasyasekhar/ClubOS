from uuid import UUID

from sqlalchemy import CheckConstraint, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MaintenanceState(Base):
    __tablename__ = "maintenance_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_maintenance_state_singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    storage_identity: Mapped[UUID] = mapped_column(server_default=text("gen_random_uuid()"))
    storage_environment: Mapped[str | None] = mapped_column(String(20))
    orphan_cursor: Mapped[str | None] = mapped_column(String(500))
