
from datetime import date
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints
from typing import Annotated

from app.models.enums import ActivityRevisionStatus

from pydantic import model_validator
from app.models.enums import ActivityReviewDecision



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

class ActivityReviewRequest(BaseModel):
    decision: ActivityReviewDecision
    comments: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def validate_comments(self):
        self.comments = self.comments.strip() if self.comments else None

        if self.decision in (
            ActivityReviewDecision.REJECTED,
            ActivityReviewDecision.CHANGES_REQUESTED,
        ) and not self.comments:
            raise ValueError("Review explanation is required.")

        return self


class ActivityReviewResponse(BaseModel):
    review_id: UUID
    revision_id: UUID
    decision: ActivityReviewDecision
    status: ActivityRevisionStatus
