import logging
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import and_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from app.models.academic_year import AcademicYear
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.club import Club
from app.models.enums import AcademicYearStatus, ClubStatus
from app.schemas.academic_year_rollover import (
    MAX_ROLLOVER_CLUBS,
    ClubRolloverItem,
    ClubRolloverRequest,
    ClubRolloverResponse,
    RolloverCandidate,
    RolloverCandidatesPage,
)
from app.services.academic_year_write_guard import require_completion_window_open
from app.services.academic_years import AcademicYearError, require_admin_school


logger = logging.getLogger(__name__)
HISTORICAL_YEAR_STATUSES = (AcademicYearStatus.CLOSED, AcademicYearStatus.ARCHIVED)
DUPLICATE_SOURCE = "A selected club has already been carried forward into this academic year."


def list_rollover_candidates(
    db: Session, admin: Account, target_year_id: UUID, *, limit: int, after: UUID | None = None,
) -> RolloverCandidatesPage:
    if not 1 <= limit <= MAX_ROLLOVER_CLUBS:
        raise AcademicYearError(422, f"Limit must be between 1 and {MAX_ROLLOVER_CLUBS}.")

    try:
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
        school_id = require_admin_school(db, admin)
        status = db.scalar(select(AcademicYear.status).where(
            AcademicYear.id == target_year_id, AcademicYear.school_id == school_id,
        ))
        if status is None:
            raise AcademicYearError(404, "Academic year not found.")
        if status != AcademicYearStatus.ACTIVE:
            raise AcademicYearError(409, "The target academic year is not active.")

        carried = aliased(Club)
        already_carried_forward = select(carried.id).where(
            carried.school_id == school_id,
            carried.academic_year_id == target_year_id,
            carried.rolled_over_from_club_id == Club.id,
        ).exists()
        query = (
            select(
                Club.id.label("source_club_id"), Club.name, Club.description, Club.category,
                AcademicYear.id.label("source_academic_year_id"),
                AcademicYear.name.label("source_academic_year_name"),
                Club.status.label("previous_status"),
                already_carried_forward.label("already_carried_forward"),
            )
            .select_from(Club)
            .join(AcademicYear, and_(
                AcademicYear.id == Club.academic_year_id, AcademicYear.school_id == Club.school_id,
            ))
            .where(
                Club.school_id == school_id,
                AcademicYear.status.in_(HISTORICAL_YEAR_STATUSES),
                Club.status == ClubStatus.ACTIVE,
            )
        )
        if after is not None:
            query = query.where(Club.id > after)

        rows = db.execute(query.order_by(Club.id).limit(limit + 1)).mappings().all()
        items = [RolloverCandidate.model_validate(row) for row in rows[:limit]]
        return RolloverCandidatesPage(items=items, next_cursor=items[-1].source_club_id if len(rows) > limit else None)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Rollover candidate lookup failed (%s)", type(exc).__name__)
        raise AcademicYearError(503, "Club rollover is temporarily unavailable.") from exc


def rollover_clubs(
    db: Session, admin: Account, target_year_id: UUID, data: ClubRolloverRequest, idempotency_key: UUID,
) -> ClubRolloverResponse:
    try:
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
        school_id = require_admin_school(db, admin, for_update=True)
        batch_id = uuid5(NAMESPACE_URL, f"clubos:club-rollover:{school_id}:{idempotency_key}")
        source_ids = set(data.source_club_ids)

        target_exists = db.scalar(select(AcademicYear.id).where(
            AcademicYear.id == target_year_id, AcademicYear.school_id == school_id,
        ))
        if target_exists is None:
            raise AcademicYearError(404, "Academic year not found.")

        previous = db.execute(select(Club.id, Club.rolled_over_from_club_id, Club.academic_year_id).where(
            Club.school_id == school_id, Club.rollover_batch_id == batch_id,
        ).order_by(Club.rolled_over_from_club_id).limit(MAX_ROLLOVER_CLUBS + 1)).all()
        if previous:
            if {row.academic_year_id for row in previous} != {target_year_id} or {
                row.rolled_over_from_club_id for row in previous
            } != source_ids:
                raise AcademicYearError(409, "Idempotency key reused with different rollover data.")
            response = ClubRolloverResponse(target_year_id=target_year_id, items=[
                ClubRolloverItem(source_club_id=row.rolled_over_from_club_id, club_id=row.id) for row in previous
            ])
            db.rollback()
            return response

        scope = (Club.school_id == school_id, Club.id.in_(source_ids))
        source_year_by_club = dict(db.execute(select(Club.id, Club.academic_year_id).where(*scope)).all())
        if source_year_by_club.keys() != source_ids:
            raise AcademicYearError(404, "One or more source clubs were not found.")

        # Discover the school-scoped source years, then lock all involved years
        # in one consistent order before taking any club locks.
        year_ids = {target_year_id, *source_year_by_club.values()}
        years_by_id = {year.id: year for year in db.scalars(
            select(AcademicYear)
            .where(AcademicYear.school_id == school_id, AcademicYear.id.in_(year_ids))
            .order_by(AcademicYear.id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )}
        target = years_by_id.get(target_year_id)
        if target is None:
            raise AcademicYearError(404, "Academic year not found.")
        if target.status != AcademicYearStatus.ACTIVE:
            raise AcademicYearError(409, "The target academic year is not active.")

        sources = db.scalars(
            select(Club).where(*scope).order_by(Club.id).with_for_update(read=True)
            .execution_options(populate_existing=True)
        ).all()
        if {club.id: club.academic_year_id for club in sources} != source_year_by_club:
            raise AcademicYearError(409, "The source clubs changed while rollover was in progress.")
        if any(
            club.status != ClubStatus.ACTIVE
            or years_by_id[club.academic_year_id].status not in HISTORICAL_YEAR_STATUSES
            for club in sources
        ):
            raise AcademicYearError(409, "Only active clubs from closed or archived academic years can be carried forward.")

        duplicate = db.scalar(select(Club.id).where(
            Club.school_id == school_id, Club.academic_year_id == target_year_id,
            Club.rolled_over_from_club_id.in_(source_ids),
        ).limit(1))
        if duplicate is not None:
            raise AcademicYearError(409, DUPLICATE_SOURCE)
        require_completion_window_open(db, target, AcademicYearError)

        carried = [
            Club(
                id=uuid4(), school_id=school_id, academic_year_id=target_year_id,
                name=club.name, description=club.description, category=club.category,
                status=ClubStatus.PENDING, rolled_over_from_club_id=club.id, rollover_batch_id=batch_id,
            )
            for club in sources
        ]
        items = [ClubRolloverItem(source_club_id=club.rolled_over_from_club_id, club_id=club.id) for club in carried]
        db.add_all([*carried, AuditLog(
            actor_account_id=admin.id,
            action="ACADEMIC_YEAR_CLUBS_ROLLED_OVER",
            entity_type="academic_year",
            entity_id=str(target_year_id),
            details={"rollover_batch_id": str(batch_id), "clubs": [item.model_dump(mode="json") for item in items]},
        )])
        db.commit()
        return ClubRolloverResponse(target_year_id=target_year_id, items=items)
    except AcademicYearError:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        if isinstance(exc, IntegrityError):
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if constraint == "uq_clubs_rollover_source_year":
                raise AcademicYearError(409, DUPLICATE_SOURCE) from exc
        logger.error("Club rollover failed (%s)", type(exc).__name__)
        raise AcademicYearError(503, "Club rollover is temporarily unavailable.") from exc
