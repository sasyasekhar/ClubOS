from datetime import date
from uuid import UUID

from sqlalchemy import Select, and_, select, tuple_
from sqlalchemy.orm import Session

from app.models.academic_year import AcademicYear
from app.models.account import Account
from app.models.activity import Activity
from app.models.activity_revision import ActivityRevision
from app.models.club import Club
from app.models.enums import AcademicYearStatus, AccountType, ActivityRevisionStatus, ClubStatus
from app.models.school import School
from app.schemas.academic_year import AcademicYearResponse
from app.schemas.academic_year_history import AcademicYearHistoryPage, HistoricalActivityPage
from app.schemas.activity import PublishedActivity
from app.services.academic_years import AcademicYearError, require_admin_school
from app.services.activity_drafts import ActivityError


HISTORICAL_YEAR_STATUSES = (AcademicYearStatus.CLOSED, AcademicYearStatus.ARCHIVED)


def _publication_school(db: Session, viewer: Account) -> UUID:
    if (
        viewer.account_type not in (AccountType.STUDENT, AccountType.TEACHER, AccountType.SCHOOL_ADMIN)
        or not viewer.is_active
        or viewer.school_id is None
    ):
        raise ActivityError(403, "Permission denied.")

    if viewer.account_type == AccountType.SCHOOL_ADMIN:
        try:
            return require_admin_school(db, viewer)
        except AcademicYearError as exc:
            raise ActivityError(exc.status_code, exc.detail) from exc

    if not db.scalar(select(School.is_active).where(School.id == viewer.school_id)):
        raise ActivityError(403, "School is inactive.")
    return viewer.school_id


def _publications(school_id: UUID) -> Select:
    return (
        select(
            ActivityRevision.activity_id,
            ActivityRevision.id.label("revision_id"),
            ActivityRevision.revision_number,
            Club.id.label("club_id"),
            Club.name.label("club_name"),
            ActivityRevision.title,
            ActivityRevision.description,
            ActivityRevision.category,
            ActivityRevision.activity_date,
        )
        .select_from(ActivityRevision)
        .join(Activity, Activity.id == ActivityRevision.activity_id)
        .join(Club, Club.id == Activity.club_id)
        .join(AcademicYear, and_(
            AcademicYear.id == Club.academic_year_id,
            AcademicYear.school_id == Club.school_id,
        ))
        .where(
            AcademicYear.school_id == school_id,
            Club.status == ClubStatus.ACTIVE,
            ActivityRevision.status == ActivityRevisionStatus.APPROVED,
            ActivityRevision.is_published,
            ActivityRevision.content_purged_at.is_(None),
            ActivityRevision.superseded_at.is_(None),
            # The public view requires retained, complete revision content.
            ActivityRevision.title.is_not(None),
            ActivityRevision.description.is_not(None),
            ActivityRevision.category.is_not(None),
            ActivityRevision.activity_date.is_not(None),
        )
        .order_by(ActivityRevision.activity_date.desc(), ActivityRevision.id.desc())
    )


def list_published_activities(db: Session, viewer: Account, limit: int = 20):
    school_id = _publication_school(db, viewer)
    query = _publications(school_id).where(AcademicYear.status == AcademicYearStatus.ACTIVE)
    return [dict(row) for row in db.execute(query.limit(limit)).mappings()]


def _cursor_key(after: str) -> tuple[date, UUID]:
    day, separator, identifier = after.partition("/")
    try:
        if not separator or len(day) != 10 or len(identifier) != 36:
            raise ValueError
        return date.fromisoformat(day), UUID(identifier)
    except ValueError as exc:
        raise ActivityError(422, "Invalid pagination cursor.") from exc


def list_historical_academic_years(
    db: Session, viewer: Account, limit: int, after: str | None,
) -> AcademicYearHistoryPage:
    school_id = _publication_school(db, viewer)
    query = select(AcademicYear).where(
        AcademicYear.school_id == school_id,
        AcademicYear.status.in_(HISTORICAL_YEAR_STATUSES),
    )
    if after is not None:
        query = query.where(tuple_(AcademicYear.start_date, AcademicYear.id) < tuple_(*_cursor_key(after)))
    years = db.scalars(query.order_by(AcademicYear.start_date.desc(), AcademicYear.id.desc()).limit(limit + 1)).all()
    items = [AcademicYearResponse.model_validate(year) for year in years[:limit]]
    return AcademicYearHistoryPage(
        items=items,
        next_cursor=f"{items[-1].start_date.isoformat()}/{items[-1].id}" if len(years) > limit else None,
    )


def list_historical_publications(
    db: Session, viewer: Account, year_id: UUID, limit: int, after: str | None,
) -> HistoricalActivityPage:
    school_id = _publication_school(db, viewer)
    history_scope = (
        AcademicYear.id == year_id,
        AcademicYear.school_id == school_id,
        AcademicYear.status.in_(HISTORICAL_YEAR_STATUSES),
    )
    if db.scalar(select(AcademicYear.id).where(*history_scope)) is None:
        raise ActivityError(404, "Academic year not found.")

    query = _publications(school_id).where(*history_scope)
    if after is not None:
        query = query.where(tuple_(ActivityRevision.activity_date, ActivityRevision.id) < tuple_(*_cursor_key(after)))
    rows = db.execute(query.limit(limit + 1)).mappings().all()
    items = [PublishedActivity.model_validate(row) for row in rows[:limit]]
    return HistoricalActivityPage(
        items=items,
        next_cursor=f"{items[-1].activity_date.isoformat()}/{items[-1].revision_id}" if len(rows) > limit else None,
    )
