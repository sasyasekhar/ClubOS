
from datetime import date
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.enums import ClubRole, MembershipStatus


class EndMembershipRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class MembershipResponse(BaseModel):
    membership_id: UUID
    club_id: UUID
    account_id: UUID
    role: ClubRole
    status: MembershipStatus
    start_date: date
    end_date: date | None
