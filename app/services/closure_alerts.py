import logging
from datetime import timezone
from uuid import UUID

from sqlalchemy import and_, false, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import Exists

from app.models.academic_year import AcademicYear
from app.models.academic_year_closure_request import AcademicYearClosureRequest
from app.models.account import Account
from app.models.activity import Activity
from app.models.activity_revision import ActivityRevision
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.enums import (
    AcademicYearStatus,
    AccountType,
    ActivityRevisionStatus,
    ClosureRequestStatus,
    ClubRole,
    ClubStatus,
    MembershipStatus,
)
from app.models.school import School
from app.schemas.closure_alerts import PendingReviewAlert, PendingReviewAlertPage
from app.services.academic_years import AcademicYearError, require_admin_school


logger = logging.getLogger(__name__)


def eligible_active_teacher_exists(*, teacher_id: UUID | None = None) -> Exists:
    """Check whether the enclosing Club has an active same-school teacher representative."""
    membership = (
        select(ClubMembership.id)
        .join(Account, Account.id == ClubMembership.account_id)
        .where(
            ClubMembership.club_id == Club.id,
            ClubMembership.role == ClubRole.TEACHER_REP,
            ClubMembership.status == MembershipStatus.ACTIVE,
            Account.school_id == Club.school_id,
            Account.account_type == AccountType.TEACHER,
            Account.is_active.is_(True),
        )
        .correlate(Club)
    )
    if teacher_id is not None:
        membership = membership.where(ClubMembership.account_id == teacher_id)
    return membership.exists()


def _alert_school(db: Session, viewer: Account, *, attention_only: bool) -> UUID:
    if viewer.account_type == AccountType.SCHOOL_ADMIN:
        return require_admin_school(db, viewer)
    if viewer.account_type != AccountType.TEACHER or attention_only:
        raise AcademicYearError(403, "Permission denied.")

    school_id = db.scalar(
        select(School.id)
        .join(Account, Account.school_id == School.id)
        .where(
            Account.id == viewer.id,
            Account.account_type == AccountType.TEACHER,
            Account.is_active.is_(True),
            School.is_active.is_(True),
        )
    )
    if school_id is None:
        raise AcademicYearError(403, "Permission denied.")
    return school_id


def list_pending_review_alerts(
    db: Session,
    viewer: Account,
    year_id: UUID,
    *,
    limit: int,
    after: UUID | None,
    attention_only: bool,
) -> PendingReviewAlertPage:
    try:
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
        school_id = _alert_school(db, viewer, attention_only=attention_only)
        year = db.scalar(select(AcademicYear.id).where(
            AcademicYear.id == year_id, AcademicYear.school_id == school_id,
        ))
        if year is None:
            raise AcademicYearError(404, "Academic year not found.")

        window = AcademicYearClosureRequest
        is_teacher = viewer.account_type == AccountType.TEACHER
        needs_attention = ~(and_(Club.status == ClubStatus.ACTIVE, eligible_active_teacher_exists()))
        query = (
            select(
                Club.id.label("club_id"), Club.name.label("club_name"),
                Activity.id.label("activity_id"), ActivityRevision.id.label("revision_id"),
                window.completion_deadline_at,
                func.greatest(
                    window.requested_at,
                    func.coalesce(ActivityRevision.submitted_at, ActivityRevision.created_at),
                ).label("created_at"),
                (false() if is_teacher else needs_attention).label("needs_attention"),
            )
            .select_from(ActivityRevision)
            .join(Activity, Activity.id == ActivityRevision.activity_id)
            .join(Club, Club.id == Activity.club_id)
            .join(AcademicYear, and_(
                AcademicYear.id == Club.academic_year_id,
                AcademicYear.school_id == Club.school_id,
            ))
            .join(window, and_(window.academic_year_id == AcademicYear.id, window.school_id == AcademicYear.school_id))
            .where(
                AcademicYear.id == year_id,
                AcademicYear.school_id == school_id,
                AcademicYear.status == AcademicYearStatus.ACTIVE,
                window.status == ClosureRequestStatus.PENDING,
                window.requested_at <= func.statement_timestamp(),
                ActivityRevision.status == ActivityRevisionStatus.PENDING,
            )
        )
        if is_teacher:
            query = query.where(
                Club.status == ClubStatus.ACTIVE,
                eligible_active_teacher_exists(teacher_id=viewer.id),
                window.completion_deadline_at > func.statement_timestamp(),
            )
        elif attention_only:
            query = query.where(needs_attention)
        if after is not None:
            query = query.where(ActivityRevision.id > after)

        rows = db.execute(query.order_by(ActivityRevision.id).limit(limit + 1)).all()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Pending-review alert lookup failed (%s)", type(exc).__name__)
        raise AcademicYearError(503, "Pending-review alerts are temporarily unavailable.") from exc

    items = [
        PendingReviewAlert(
            academic_year_id=year_id,
            club_id=row.club_id,
            club_name=row.club_name,
            activity_id=row.activity_id,
            revision_id=row.revision_id,
            message=f"{row.club_name} has an activity awaiting review.",
            completion_deadline_at=row.completion_deadline_at.astimezone(timezone.utc),
            created_at=row.created_at.astimezone(timezone.utc),
            needs_attention=row.needs_attention,
        )
        for row in rows[:limit]
    ]
    return PendingReviewAlertPage(
        items=items,
        next_cursor=items[-1].revision_id if len(rows) > limit else None,
    )
