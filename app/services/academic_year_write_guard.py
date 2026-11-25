from collections.abc import Callable
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.academic_year import AcademicYear
from app.models.academic_year_closure_request import AcademicYearClosureRequest
from app.models.club import Club
from app.models.enums import AcademicYearStatus, ClosureRequestStatus


COMPLETION_WINDOW_ENDED_CODE = "ACADEMIC_YEAR_COMPLETION_WINDOW_ENDED"


def require_completion_window_open(
    db: Session,
    year: AcademicYear,
    error: Callable[[int, dict[str, str]], Exception],
) -> datetime:
    """Admit a write using PostgreSQL time after locking the year and target rows."""
    request_scope = and_(
        AcademicYearClosureRequest.school_id == AcademicYear.school_id,
        AcademicYearClosureRequest.academic_year_id == AcademicYear.id,
        AcademicYearClosureRequest.status.in_((ClosureRequestStatus.PENDING, ClosureRequestStatus.CONFIRMED)),
    )
    checked_at, deadline_at, finalized_at = db.execute(
        select(
            func.clock_timestamp(),
            AcademicYearClosureRequest.completion_deadline_at,
            AcademicYearClosureRequest.finalized_at,
        )
        .select_from(AcademicYear)
        .outerjoin(AcademicYearClosureRequest, request_scope)
        .where(AcademicYear.id == year.id, AcademicYear.school_id == year.school_id)
    ).one()
    if deadline_at is not None and (
        checked_at.astimezone(timezone.utc) >= deadline_at.astimezone(timezone.utc) or finalized_at is not None
    ):
        raise error(409, {
            "code": COMPLETION_WINDOW_ENDED_CODE,
            "message": "The academic-year completion window has ended.",
            "completion_deadline_at": deadline_at.astimezone(timezone.utc).isoformat(),
        })
    return checked_at


def lock_academic_year_for_write(
    db: Session, school_id: UUID, year_id: UUID | None = None, *, exclusive: bool = False,
) -> AcademicYear | None:
    """Lock a scoped year, defaulting to a shared lock on the school's active year.

    Callers hold the lock through their transaction and check the returned status
    before mutation. Selecting by ID also supports read-only idempotent replays.
    """
    query = select(AcademicYear).where(AcademicYear.school_id == school_id)
    if year_id is None:
        query = query.where(AcademicYear.status == AcademicYearStatus.ACTIVE)
    else:
        query = query.where(AcademicYear.id == year_id)

    return db.scalar(query.with_for_update(read=not exclusive).execution_options(populate_existing=True))


def lock_club_for_year_write(
    db: Session,
    school_id: UUID,
    club_id: UUID,
    *,
    creator_id: UUID | None = None,
) -> tuple[Club, AcademicYear] | None:
    """Resolve a scoped club, then lock its year before revalidating and locking the club."""
    scope = [Club.id == club_id, Club.school_id == school_id]
    if creator_id is not None:
        scope.append(Club.created_by == creator_id)

    year_id = db.scalar(select(Club.academic_year_id).where(*scope))
    if year_id is None:
        return None

    year = lock_academic_year_for_write(db, school_id, year_id)
    if year is None:
        return None

    club = db.scalar(
        select(Club)
        .where(*scope, Club.academic_year_id == year.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if club is None:
        return None
    return club, year
