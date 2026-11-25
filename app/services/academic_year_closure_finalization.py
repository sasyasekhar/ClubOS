from uuid import UUID

from sqlalchemy import DateTime, and_, func, insert, literal, select, text, update
from sqlalchemy.orm import Session

from app.models.academic_year import AcademicYear
from app.models.academic_year_closure_request import AcademicYearClosureRequest
from app.models.activity import Activity
from app.models.activity_deadline_rejection import DEADLINE_EXPIRY_REASON, ActivityDeadlineRejection
from app.models.activity_revision import ActivityRevision
from app.models.club import Club
from app.models.enums import AcademicYearStatus, ActivityRevisionStatus, ClosureRequestStatus
from app.services.academic_year_write_guard import lock_academic_year_for_write


DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 5000
DEFAULT_WINDOW_LIMIT = 100
MAX_WINDOW_LIMIT = 1000


def finalize_locked_closure_window(
    db: Session,
    year: AcademicYear,
    request: AcademicYearClosureRequest,
    *,
    batch_size: int | None = None,
) -> int:
    """Reject due revisions inside the caller's exclusive year and request locks."""
    if request.academic_year_id != year.id or request.school_id != year.school_id:
        raise ValueError("The closure request must belong to the locked year.")
    if batch_size is not None and not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"Batch size must be between 1 and {MAX_BATCH_SIZE}.")
    if (
        year.status != AcademicYearStatus.ACTIVE
        or request.status != ClosureRequestStatus.PENDING
        or request.finalized_at is not None
    ):
        return 0

    resolved_at = db.execute(select(func.clock_timestamp())).scalar_one()
    if resolved_at < request.completion_deadline_at:
        return 0

    pending = (
        select(ActivityRevision.id)
        .join(Activity, Activity.id == ActivityRevision.activity_id)
        .join(Club, Club.id == Activity.club_id)
        .where(
            Club.school_id == year.school_id,
            Club.academic_year_id == year.id,
            ActivityRevision.status == ActivityRevisionStatus.PENDING,
        )
    )
    batch = pending if batch_size is None else pending.order_by(ActivityRevision.id).limit(batch_size)
    rejected = (
        update(ActivityRevision.__table__)
        .where(ActivityRevision.id.in_(batch), ActivityRevision.status == ActivityRevisionStatus.PENDING)
        .values(status=ActivityRevisionStatus.REJECTED)
        .returning(ActivityRevision.id.label("revision_id"))
        .cte("expired_revisions")
    )
    # Record only revisions changed by the update. Neither operation can commit alone.
    recorded = (
        insert(ActivityDeadlineRejection.__table__)
        .from_select(
            ["revision_id", "closure_request_id", "resolved_at", "reason"],
            select(
                rejected.c.revision_id,
                literal(request.id),
                literal(resolved_at, DateTime(timezone=True)),
                literal(DEADLINE_EXPIRY_REASON),
            ),
        )
        .returning(ActivityDeadlineRejection.revision_id)
        .cte("recorded_deadline_rejections")
    )
    count = db.execute(select(func.count()).select_from(recorded)).scalar_one()

    if not db.scalar(select(pending.exists())):
        request.finalized_at = resolved_at
    return count


def finalize_expired_closure_window(
    db: Session,
    school_id: UUID,
    year_id: UUID,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Process one scoped window in a fresh transaction, committing at most one batch."""
    with db.begin():
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
        year = lock_academic_year_for_write(db, school_id, year_id, exclusive=True)
        if year is None or year.status != AcademicYearStatus.ACTIVE:
            return 0

        request = db.scalar(
            select(AcademicYearClosureRequest)
            .where(
                AcademicYearClosureRequest.school_id == school_id,
                AcademicYearClosureRequest.academic_year_id == year_id,
                AcademicYearClosureRequest.status == ClosureRequestStatus.PENDING,
                AcademicYearClosureRequest.finalized_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if request is None:
            return 0
        return finalize_locked_closure_window(db, year, request, batch_size=batch_size)


def process_expired_closure_windows(
    db: Session,
    *,
    limit: int = DEFAULT_WINDOW_LIMIT,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[int, int]:
    """Select a bounded set of due windows and commit one batch per window."""
    if not 1 <= limit <= MAX_WINDOW_LIMIT:
        raise ValueError(f"Window limit must be between 1 and {MAX_WINDOW_LIMIT}.")
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"Batch size must be between 1 and {MAX_BATCH_SIZE}.")

    with db.begin():
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
        candidates = db.execute(due_closure_windows(limit)).all()

    rejected = sum(
        finalize_expired_closure_window(db, school_id, year_id, batch_size=batch_size)
        for school_id, year_id in candidates
    )
    return len(candidates), rejected


def due_closure_windows(limit: int):
    request = AcademicYearClosureRequest
    return (
        select(request.school_id, request.academic_year_id)
        .join(AcademicYear, and_(AcademicYear.id == request.academic_year_id, AcademicYear.school_id == request.school_id))
        .where(
            request.status == literal(ClosureRequestStatus.PENDING.value, literal_execute=True),
            request.finalized_at.is_(None),
            request.completion_deadline_at <= func.statement_timestamp(),
            AcademicYear.status == AcademicYearStatus.ACTIVE,
        )
        .order_by(request.completion_deadline_at, request.id)
        .limit(limit)
    )
