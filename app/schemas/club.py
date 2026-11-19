
from uuid import UUID
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, model_validator
from app.models.enums import ClubReviewDecision, ClubStatus


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


class ClubReviewRequest(BaseModel):
    decision: ClubReviewDecision
    comments: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def validate_comments(self):
        self.comments = (
            self.comments.strip() if self.comments else None
        )

        if self.decision in (
            ClubReviewDecision.REJECTED,
            ClubReviewDecision.CHANGES_REQUESTED,
        ) and not self.comments:
            raise ValueError("Review comments are required.")

        return self


class ClubReviewResponse(BaseModel):
    id: UUID
    club_id: UUID
    decision: ClubReviewDecision
    club_status: ClubStatus
