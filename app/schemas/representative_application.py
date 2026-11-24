from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from app.models.representative_application import RepresentativeApplicationStatus


MAX_REPRESENTATIVE_PAGE_SIZE = 50


class RepresentativePosition(BaseModel):
    club_id: UUID
    academic_year_id: UUID
    name: str
    description: str | None
    category: str


class RepresentativeApplicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    club_id: UUID
    applicant_account_id: UUID
    status: RepresentativeApplicationStatus
    submitted_at: datetime
    reviewed_at: datetime | None
    review_comments: str | None


class ReviewedRepresentativeApplicationResponse(RepresentativeApplicationResponse):
    reviewed_by_account_id: UUID | None


class RepresentativeApplicantResponse(ReviewedRepresentativeApplicationResponse):
    applicant_name: str


Item = TypeVar("Item", bound=BaseModel)


class RepresentativePage(BaseModel, Generic[Item]):
    items: list[Item]
    next_cursor: UUID | None


class RepresentativeApplicationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal[
        RepresentativeApplicationStatus.SELECTED,
        RepresentativeApplicationStatus.REJECTED,
        RepresentativeApplicationStatus.CHANGES_REQUESTED,
    ]
    comments: Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)] | None = None

    @model_validator(mode="after")
    def validate_comments(self):
        self.comments = self.comments or None
        if self.decision != RepresentativeApplicationStatus.SELECTED and self.comments is None:
            raise ValueError("Review comments are required.")
        if self.comments and "\x00" in self.comments:
            raise ValueError("Review comments contain an invalid character.")
        return self
