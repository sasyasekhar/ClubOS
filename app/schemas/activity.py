
from datetime import date
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints
from typing import Annotated

from app.models.enums import ActivityRevisionStatus


CleanTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=200),
]

CleanCategory = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=2, max_length=100),
]


class ActivityDraftCreate(BaseModel):
    title: CleanTitle
    activity_date: date
    description: str = Field(min_length=1, max_length=10000)
    category: CleanCategory


class ActivityDraftResponse(BaseModel):
    activity_id: UUID
    revision_id: UUID
    revision_number: int
    status: ActivityRevisionStatus
