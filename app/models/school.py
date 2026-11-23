import uuid

from sqlalchemy import (
    String,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class School(Base):
    __tablename__ = "schools"
    __table_args__ = (
        CheckConstraint(
            "(closure_teacher_account_id IS NULL) = (closure_teacher_designation_id IS NULL)",
            name="ck_school_closure_teacher_designation",
        ),
        ForeignKeyConstraint(
            ["id", "closure_teacher_account_id"],
            ["accounts.school_id", "accounts.id"],
            name="fk_school_closure_teacher_same_school",
            use_alter=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )

    school_code: Mapped[str] = mapped_column(
        String(30),
        unique=True,
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    closure_teacher_account_id: Mapped[uuid.UUID | None]
    closure_teacher_designation_id: Mapped[uuid.UUID | None]

    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
