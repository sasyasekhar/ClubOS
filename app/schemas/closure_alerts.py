from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class PendingReviewAlert(BaseModel):
    academic_year_id: UUID
    club_id: UUID
    club_name: str
    activity_id: UUID
    revision_id: UUID
    message: str
    completion_deadline_at: datetime
    created_at: datetime
    needs_attention: bool


class PendingReviewAlertPage(BaseModel):
    items: list[PendingReviewAlert]
    next_cursor: UUID | None
