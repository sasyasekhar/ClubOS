from datetime import date, datetime
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.enums import (
    AccountType, ActivityReviewDecision, ActivityRevisionStatus, ClubRole, ClubStatus, MembershipStatus, ReviewMode,
)
from app.schemas.activity import ActivityAttachmentResponse, PublishedActivity
from app.schemas.representative_application import RepresentativeApplicationResponse, RepresentativePage
from app.schemas.teacher_invitation import PendingTeacherInvitation


Item = TypeVar("Item", bound=BaseModel)


class DashboardPage(BaseModel, Generic[Item]):
    items: list[Item]
    next_cursor: UUID | None


class DashboardQuery(BaseModel):
    limit: int = Field(default=20, ge=1, le=50)
    memberships_after: UUID | None = None
    activities_after: UUID | None = None
    applications_after: UUID | None = None
    invitations_after: UUID | None = None
    history_after: UUID | None = None


class RevisionDetailQuery(BaseModel):
    limit: int = Field(default=20, ge=1, le=50)
    attachments_after: UUID | None = None
    reviews_after: UUID | None = None


class DashboardClub(BaseModel):
    club_id: UUID
    academic_year_id: UUID
    name: str
    category: str
    description: str | None
    status: ClubStatus


class DashboardMembership(BaseModel):
    membership_id: UUID
    club_id: UUID
    club_name: str
    academic_year_id: UUID
    role: ClubRole
    status: MembershipStatus
    start_date: date
    end_date: date | None


class DashboardRevision(BaseModel):
    revision_id: UUID
    activity_id: UUID
    club_id: UUID
    club_name: str
    revision_number: int
    status: ActivityRevisionStatus
    is_published: bool
    title: str | None
    category: str | None
    activity_date: date | None
    submitted_at: datetime | None


class ActivityReviewHistoryEntry(BaseModel):
    review_id: UUID
    revision_id: UUID
    activity_id: UUID
    club_id: UUID
    decision: ActivityReviewDecision
    comments: str | None
    reviewed_at: datetime
    review_mode: ReviewMode


class DashboardRevisionDetail(DashboardRevision):
    description: str | None
    attachments: DashboardPage[ActivityAttachmentResponse]
    reviews: DashboardPage[ActivityReviewHistoryEntry]


class MemberDashboard(BaseModel):
    memberships: DashboardPage[DashboardMembership]
    published_activities: list[PublishedActivity]


class StudentDashboard(MemberDashboard):
    role: Literal[AccountType.STUDENT] = AccountType.STUDENT
    representative_applications: RepresentativePage[RepresentativeApplicationResponse]
    activity_revisions: DashboardPage[DashboardRevision]


class TeacherDashboard(MemberDashboard):
    role: Literal[AccountType.TEACHER] = AccountType.TEACHER
    teacher_invitations: DashboardPage[PendingTeacherInvitation]
    pending_reviews: DashboardPage[DashboardRevision]
    review_history: DashboardPage[ActivityReviewHistoryEntry]


Dashboard = Annotated[StudentDashboard | TeacherDashboard, Field(discriminator="role")]
