import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, and_, case, false, func, select, text, true
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.academic_year import AcademicYear
from app.models.academic_year_closure_request import AcademicYearClosureRequest
from app.models.account import Account
from app.models.activity import Activity
from app.models.activity_deadline_rejection import ActivityDeadlineRejection
from app.models.activity_revision import ActivityRevision
from app.models.club import Club
from app.models.enums import AcademicYearStatus, ActivityRevisionStatus, ClosureRequestStatus, ClubStatus
from app.schemas.academic_year import (
    AcademicYearClosureReadiness,
    ActivityRevisionCounts,
    ClosureWindowStatus,
    PendingReviewBlocker,
)
from app.services.academic_years import AcademicYearError, require_admin_school
from app.services.closure_alerts import eligible_active_teacher_exists


logger = logging.getLogger(__name__)
PENDING_BLOCKER_LIMIT = 50


def _selected_year(school_id: UUID, year_id: UUID):
    return (
        select(AcademicYear.id, AcademicYear.school_id, AcademicYear.status)
        .where(AcademicYear.id == year_id, AcademicYear.school_id == school_id)
        .cte("selected_year")
    )


def _year_revisions(year) -> Select:
    return (
        select(
            Club.id.label("club_id"),
            Activity.id.label("activity_id"),
            ActivityRevision.id.label("revision_id"),
            ActivityRevision.status,
        )
        .join(Activity, Activity.id == ActivityRevision.activity_id)
        .join(Club, Club.id == Activity.club_id)
        .join(year, and_(Club.academic_year_id == year.c.id, Club.school_id == year.c.school_id))
    )


def count_pending_reviews(db: Session, school_id: UUID, year_id: UUID) -> int:
    pending = _year_revisions(_selected_year(school_id, year_id)).where(
        ActivityRevision.status == ActivityRevisionStatus.PENDING
    )
    return db.execute(select(func.count()).select_from(pending.subquery())).scalar_one()


def completion_window_status(
    request_status: ClosureRequestStatus | None,
    deadline: datetime | None,
    finalized_at: datetime | None,
    as_of: datetime,
) -> ClosureWindowStatus:
    if deadline is None:
        return ClosureWindowStatus.NOT_REQUESTED
    if request_status == ClosureRequestStatus.CONFIRMED:
        return ClosureWindowStatus.CONFIRMED
    if finalized_at is not None:
        return ClosureWindowStatus.FINALIZED
    if deadline <= as_of:
        return ClosureWindowStatus.EXPIRED
    return ClosureWindowStatus.OPEN


def _closure_readiness_query(school_id: UUID, year_id: UUID) -> Select:
    year = _selected_year(school_id, year_id)
    request = AcademicYearClosureRequest
    revisions = _year_revisions(year).add_columns(
        case(
            (ActivityRevision.status == ActivityRevisionStatus.PENDING,
             and_(Club.status == ClubStatus.ACTIVE, eligible_active_teacher_exists())),
            else_=false(),
        ).label("can_review")
    ).cte("year_revisions")
    pending = revisions.c.status == ActivityRevisionStatus.PENDING
    counts = select(
        *(func.count().filter(revisions.c.status == status).label(status.value) for status in ActivityRevisionStatus),
        func.count().filter(pending, ~revisions.c.can_review).label("pending_review_attention_count"),
    ).subquery()
    blockers = (
        select(revisions.c.club_id, revisions.c.activity_id, revisions.c.revision_id)
        .where(pending)
        .order_by(revisions.c.revision_id)
        .limit(PENDING_BLOCKER_LIMIT)
        .subquery()
    )
    auto_rejections = select(func.count().label("auto_rejected_count")).where(
        ActivityDeadlineRejection.closure_request_id == request.id,
    ).correlate(request).lateral()
    # All progress fields and the bounded blocker list use the same statement snapshot.
    return (
        select(
            year.c.status.label("year_status"),
            request.id.label("closure_request_id"),
            request.status.label("request_status"),
            request.completion_deadline_at,
            request.finalized_at,
            func.statement_timestamp().label("as_of"),
            auto_rejections.c.auto_rejected_count,
            *counts.c,
            *blockers.c,
        )
        .select_from(year.outerjoin(request, and_(
            request.school_id == year.c.school_id,
            request.academic_year_id == year.c.id,
            request.status.in_((ClosureRequestStatus.PENDING, ClosureRequestStatus.CONFIRMED)),
        )).join(auto_rejections, true()).join(counts, true()).outerjoin(blockers, true()))
        .order_by(blockers.c.revision_id)
    )


def check_closure_readiness(
    db: Session, admin: Account, year_id: UUID
) -> AcademicYearClosureReadiness:
    try:
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
        school_id = require_admin_school(db, admin)
        rows = db.execute(_closure_readiness_query(school_id, year_id)).mappings().all()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Academic-year closure readiness failed (%s)", type(exc).__name__)
        raise AcademicYearError(503, "Closure readiness is temporarily unavailable.") from exc

    if not rows:
        raise AcademicYearError(404, "Academic year not found.")

    summary = rows[0]
    counts = ActivityRevisionCounts(**{
        status.value: summary[status.value] for status in ActivityRevisionStatus
    })
    window_status = completion_window_status(
        summary["request_status"], summary["completion_deadline_at"], summary["finalized_at"], summary["as_of"],
    )
    active = summary["year_status"] == AcademicYearStatus.ACTIVE
    can_confirm = active and window_status in (ClosureWindowStatus.EXPIRED, ClosureWindowStatus.FINALIZED)
    return AcademicYearClosureReadiness(
        academic_year_id=year_id,
        status=summary["year_status"],
        counts=counts,
        can_close=can_confirm,
        pending_review_blocker_count=counts.PENDING,
        pending_review_blockers=[
            PendingReviewBlocker(
                club_id=row["club_id"], activity_id=row["activity_id"], revision_id=row["revision_id"]
            )
            for row in rows if row["revision_id"] is not None
        ],
        pending_review_attention_count=summary["pending_review_attention_count"],
        auto_rejected_count=summary["auto_rejected_count"],
        closure_request_id=summary["closure_request_id"],
        completion_deadline_at=summary["completion_deadline_at"],
        finalized_at=summary["finalized_at"],
        window_status=window_status,
        can_initiate=active and window_status == ClosureWindowStatus.NOT_REQUESTED,
        can_confirm=can_confirm,
    )
