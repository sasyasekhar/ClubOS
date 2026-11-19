
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from app.models.enums import MembershipStatus


class TeacherInvitationDecision(BaseModel):
    decision: Literal["ACCEPT", "DECLINE"]


class TeacherInvitationResponse(BaseModel):
    membership_id: UUID
    club_id: UUID
    status: MembershipStatus


class PendingTeacherInvitation(BaseModel):
    membership_id: UUID
    club_id: UUID
    club_name: str
