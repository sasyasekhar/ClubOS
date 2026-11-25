from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app.models.academic_year import AcademicYear
from app.models.account import Account
from app.models.activity import Activity
from app.models.activity_attachment import ActivityAttachment
from app.models.activity_review import ActivityReview
from app.models.activity_revision import ActivityRevision
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.enums import AcademicYearStatus, AccountType, ActivityRevisionStatus, ClubRole, ClubStatus, MembershipStatus
from app.models.representative_application import RepresentativeApplication
from app.schemas.activity import ActivityAttachmentResponse, PublishedActivity
from app.schemas.dashboard import (
    ActivityReviewHistoryEntry,
    Dashboard,
    DashboardClub,
    DashboardMembership,
    DashboardPage,
    DashboardQuery,
    DashboardRevision,
    DashboardRevisionDetail,
    RevisionDetailQuery,
    StudentDashboard,
    TeacherDashboard,
)
from app.schemas.teacher_invitation import PendingTeacherInvitation
from app.services.activity_drafts import ActivityError
from app.services.activity_publications import list_published_activities
from app.services.representative_applications import list_student_representative_applications
from app.services.teacher_invitation import list_teacher_invitations


REPRESENTATIVE_ROLES = {AccountType.STUDENT: ClubRole.STUDENT_REP, AccountType.TEACHER: ClubRole.TEACHER_REP}
Item = TypeVar("Item", bound=BaseModel)

REVISION_FIELDS = (
    ActivityRevision.id.label("revision_id"),
    ActivityRevision.activity_id,
    ActivityRevision.revision_number,
    ActivityRevision.status,
    ActivityRevision.is_published,
    ActivityRevision.title,
    ActivityRevision.category,
    ActivityRevision.activity_date,
    ActivityRevision.submitted_at,
    Activity.club_id,
    Club.name.label("club_name"),
)
REVIEW_FIELDS = (
    ActivityReview.id.label("review_id"),
    ActivityReview.revision_id,
    ActivityRevision.activity_id,
    Activity.club_id,
    ActivityReview.decision,
    ActivityReview.comments,
    ActivityReview.reviewed_at,
    ActivityReview.review_mode,
)


def _viewer_role(viewer: Account) -> ClubRole:
    role = REPRESENTATIVE_ROLES.get(viewer.account_type)
    if role is None or not viewer.is_active or viewer.school_id is None:
        raise ActivityError(403, "Permission denied.")
    return role


def _own_membership(viewer: Account, role: ClubRole):
    return (ClubMembership.club_id == Club.id, ClubMembership.account_id == viewer.id, ClubMembership.role == role)


def _page(
    db: Session, query: Select, identifier, model: type[Item], limit: int, after: UUID | None,
) -> DashboardPage[Item]:
    if after is not None:
        query = query.where(identifier > after)
    rows = db.execute(query.add_columns(identifier.label("cursor")).order_by(identifier).limit(limit + 1)).mappings().all()
    return DashboardPage[model](
        items=[model.model_validate(row) for row in rows[:limit]],
        next_cursor=rows[limit - 1]["cursor"] if len(rows) > limit else None,
    )


def _club_revisions(viewer: Account, role: ClubRole, *columns) -> Select:
    active_membership = select(ClubMembership.id).where(
        *_own_membership(viewer, role), ClubMembership.status == MembershipStatus.ACTIVE,
    ).exists()
    return (
        select(*columns)
        .select_from(ActivityRevision)
        .join(Activity, Activity.id == ActivityRevision.activity_id)
        .join(Club, Club.id == Activity.club_id)
        .join(AcademicYear, AcademicYear.id == Club.academic_year_id)
        .where(Club.school_id == viewer.school_id, Club.status == ClubStatus.ACTIVE, active_membership)
    )


def get_dashboard(db: Session, viewer: Account, query: DashboardQuery) -> Dashboard:
    role = _viewer_role(viewer)
    membership_query = (
        select(
            ClubMembership.id.label("membership_id"), ClubMembership.club_id, Club.name.label("club_name"),
            Club.academic_year_id, ClubMembership.role, ClubMembership.status,
            ClubMembership.start_date, ClubMembership.end_date,
        )
        .join(Club, Club.id == ClubMembership.club_id)
        .where(
            Club.school_id == viewer.school_id, *_own_membership(viewer, role),
            ClubMembership.status.in_((MembershipStatus.ACTIVE, MembershipStatus.ENDED)),
        )
    )
    memberships = _page(db, membership_query, ClubMembership.id, DashboardMembership, query.limit, query.memberships_after)
    publications = [PublishedActivity.model_validate(row) for row in list_published_activities(db, viewer, query.limit)]
    revisions = _club_revisions(viewer, role, *REVISION_FIELDS).where(AcademicYear.status == AcademicYearStatus.ACTIVE)

    if viewer.account_type == AccountType.STUDENT:
        return StudentDashboard(
            memberships=memberships,
            published_activities=publications,
            representative_applications=list_student_representative_applications(
                db, viewer, limit=query.limit, after=query.applications_after,
            ),
            activity_revisions=_page(
                db, revisions, ActivityRevision.id, DashboardRevision, query.limit, query.activities_after,
            ),
        )

    invitations = list_teacher_invitations(db, viewer, limit=query.limit + 1, after=query.invitations_after)
    history = _club_revisions(viewer, role, *REVIEW_FIELDS).join(
        ActivityReview, ActivityReview.revision_id == ActivityRevision.id,
    ).where(or_(ActivityReview.actor_account_id == viewer.id, ActivityReview.represented_account_id == viewer.id))
    return TeacherDashboard(
        memberships=memberships,
        published_activities=publications,
        teacher_invitations=DashboardPage[PendingTeacherInvitation](
            items=[PendingTeacherInvitation.model_validate(row) for row in invitations[:query.limit]],
            next_cursor=invitations[query.limit - 1]["membership_id"] if len(invitations) > query.limit else None,
        ),
        pending_reviews=_page(
            db, revisions.where(ActivityRevision.status == ActivityRevisionStatus.PENDING),
            ActivityRevision.id, DashboardRevision, query.limit, query.activities_after,
        ),
        review_history=_page(db, history, ActivityReview.id, ActivityReviewHistoryEntry, query.limit, query.history_after),
    )


def get_dashboard_club(db: Session, viewer: Account, club_id: UUID) -> DashboardClub:
    role = _viewer_role(viewer)
    membership = select(ClubMembership.id).where(*_own_membership(viewer, role))
    active_membership = membership.where(ClubMembership.status == MembershipStatus.ACTIVE).exists()

    if viewer.account_type == AccountType.STUDENT:
        own_application = select(RepresentativeApplication.id).where(
            RepresentativeApplication.club_id == Club.id,
            RepresentativeApplication.school_id == Club.school_id,
            RepresentativeApplication.applicant_account_id == viewer.id,
        ).exists()
        related = or_(active_membership, own_application, Club.created_by == viewer.id)
    else:
        own_invitation = membership.where(ClubMembership.status == MembershipStatus.PENDING).exists()
        related = or_(active_membership, own_invitation & Club.status.not_in((ClubStatus.REJECTED, ClubStatus.ARCHIVED)))

    row = db.execute(select(
        Club.id.label("club_id"), Club.academic_year_id, Club.name, Club.category, Club.description, Club.status,
    ).where(Club.id == club_id, Club.school_id == viewer.school_id, related)).mappings().one_or_none()
    if row is None:
        raise ActivityError(404, "Club not found.")
    return DashboardClub.model_validate(row)


def authorized_dashboard_revision(
    db: Session, viewer: Account, club_id: UUID, activity_id: UUID, revision_id: UUID,
):
    role = _viewer_role(viewer)
    scope = _club_revisions(viewer, role, *REVISION_FIELDS, ActivityRevision.description).where(
        Club.id == club_id, Activity.id == activity_id, ActivityRevision.id == revision_id,
    )
    if viewer.account_type == AccountType.TEACHER:
        scope = scope.where(ActivityRevision.status != ActivityRevisionStatus.DRAFT)
    else:
        scope = scope.where(or_(
            ActivityRevision.status != ActivityRevisionStatus.DRAFT, AcademicYear.status == AcademicYearStatus.ACTIVE,
        ))
    row = db.execute(scope.with_for_update(read=True, of=ActivityRevision)).mappings().one_or_none()
    if row is None:
        raise ActivityError(404, "Revision not found.")
    return row


def get_dashboard_revision(
    db: Session, viewer: Account, club_id: UUID, activity_id: UUID, revision_id: UUID, query: RevisionDetailQuery,
) -> DashboardRevisionDetail:
    revision = authorized_dashboard_revision(db, viewer, club_id, activity_id, revision_id)
    attachments = _page(
        db, select(
            ActivityAttachment.id, ActivityAttachment.revision_id, ActivityAttachment.file_name, ActivityAttachment.file_type,
        ).where(ActivityAttachment.revision_id == revision_id, ActivityAttachment.scan_passed_at.is_not(None)),
        ActivityAttachment.id, ActivityAttachmentResponse, query.limit, query.attachments_after,
    )
    reviews = _page(
        db, select(*REVIEW_FIELDS)
        .select_from(ActivityReview)
        .join(ActivityRevision, ActivityRevision.id == ActivityReview.revision_id)
        .join(Activity, Activity.id == ActivityRevision.activity_id)
        .where(ActivityReview.revision_id == revision_id),
        ActivityReview.id, ActivityReviewHistoryEntry, query.limit, query.reviews_after,
    )
    return DashboardRevisionDetail(**revision, attachments=attachments, reviews=reviews)
