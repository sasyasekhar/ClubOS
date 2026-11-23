import logging
from uuid import UUID

from sqlalchemy import Select, and_, func, select, text, true
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.academic_year import AcademicYear
from app.models.account import Account
from app.models.activity import Activity
from app.models.activity_revision import ActivityRevision
from app.models.club import Club
from app.models.enums import AcademicYearStatus, ActivityRevisionStatus
from app.schemas.academic_year import (
    AcademicYearClosureReadiness,
    ActivityRevisionCounts,
    PendingReviewBlocker,
)
from app.services.academic_years import AcademicYearError, require_admin_school


logger = logging.getLogger(__name__)
PENDING_BLOCKER_LIMIT = 50


def _closure_readiness_query(school_id: UUID, year_id: UUID) -> Select:
    year = (
        select(AcademicYear.id, AcademicYear.school_id, AcademicYear.status)
        .where(AcademicYear.id == year_id, AcademicYear.school_id == school_id)
        .cte("selected_year")
    )
    revisions = (
        select(
            Club.id.label("club_id"),
            Activity.id.label("activity_id"),
            ActivityRevision.id.label("revision_id"),
            ActivityRevision.status,
        )
        .join(Activity, Activity.id == ActivityRevision.activity_id)
        .join(Club, Club.id == Activity.club_id)
        .join(year, and_(Club.academic_year_id == year.c.id, Club.school_id == year.c.school_id))
        .cte("year_revisions")
    )
    counts = select(*(
        func.count().filter(revisions.c.status == status).label(status.value)
        for status in ActivityRevisionStatus
    )).subquery()
    blockers = (
        select(revisions.c.club_id, revisions.c.activity_id, revisions.c.revision_id)
        .where(revisions.c.status == ActivityRevisionStatus.PENDING)
        .order_by(revisions.c.revision_id)
        .limit(PENDING_BLOCKER_LIMIT)
        .subquery()
    )
    # Read year status, counts, and blocker IDs from the same statement snapshot.
    return (
        select(year.c.status.label("year_status"), *counts.c, *blockers.c)
        .select_from(year.join(counts, true()).outerjoin(blockers, true()))
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
    return AcademicYearClosureReadiness(
        academic_year_id=year_id,
        status=summary["year_status"],
        counts=counts,
        can_close=summary["year_status"] == AcademicYearStatus.ACTIVE and counts.PENDING == 0,
        pending_review_blocker_count=counts.PENDING,
        pending_review_blockers=[
            PendingReviewBlocker(
                club_id=row["club_id"], activity_id=row["activity_id"], revision_id=row["revision_id"]
            )
            for row in rows if row["revision_id"] is not None
        ],
    )
