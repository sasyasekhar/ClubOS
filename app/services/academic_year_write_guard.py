from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.academic_year import AcademicYear
from app.models.club import Club
from app.models.enums import AcademicYearStatus


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
