
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from app.models.enums import MembershipStatus


class TeacherInvitationDecision(BaseModel):
    decision: Literal["ACCEPT", "DECLINE"]
    membership_id: UUID | None = None


class TeacherInvitationResponse(BaseModel):
    membership_id: UUID
    club_id: UUID
    status: MembershipStatus


class PendingTeacherInvitation(BaseModel):
    membership_id: UUID
    club_id: UUID
    club_name: str


class EligibleTeacher(BaseModel):
    teacher_account_id: UUID
    full_name: str


class EligibleTeachersPage(BaseModel):
    items: list[EligibleTeacher]
    next_cursor: UUID | None
