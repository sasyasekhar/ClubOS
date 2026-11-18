
from uuid import UUID
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints
from app.models.enums import ClubStatus


ClubName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=150),
]

Category = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=100),
]


class ClubRegistrationRequest(BaseModel):
    name: ClubName
    description: str | None = Field(default=None, max_length=5000)
    category: Category
    teacher_account_id: UUID


class ClubRegistrationResponse(BaseModel):
    id: UUID
    school_id: UUID
    academic_year_id: UUID
    status: ClubStatus
    teacher_account_id: UUID
