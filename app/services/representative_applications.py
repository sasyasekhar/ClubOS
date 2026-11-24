import logging
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.academic_year import AcademicYear
from app.models.academic_year_closure_request import AcademicYearClosureRequest
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.club import Club
from app.models.enums import AcademicYearStatus, AccountType, ClosureRequestStatus, ClubStatus
from app.models.representative_application import RepresentativeApplication, RepresentativeApplicationStatus
from app.models.school import School
from app.models.school_admin import SchoolAdmin
from app.schemas.representative_application import (
    MAX_REPRESENTATIVE_PAGE_SIZE,
    RepresentativeApplicantResponse,
    RepresentativeApplicationResponse,
    RepresentativeApplicationReviewRequest,
    RepresentativePage,
    RepresentativePosition,
    ReviewedRepresentativeApplicationResponse,
)
from app.services.academic_year_write_guard import lock_club_for_year_write, require_completion_window_open
from app.services.club_registration import ClubRequestError


logger = logging.getLogger(__name__)
APPLICATION_SUBMITTED = "CLUB_REPRESENTATIVE_APPLICATION_SUBMITTED"
APPLICATION_REVIEWED = "CLUB_REPRESENTATIVE_APPLICATION_REVIEWED"
APPLICATION_ENTITY_TYPE = "club_representative_application"
DUPLICATE_APPLICATION = "You already applied to represent this club."
POSITION_FILLED = "A student has already been selected to represent this club."
IDEMPOTENCY_CONFLICT = "Idempotency key reused with different review data."


@contextmanager
def _application_operation(db: Session, *, write: bool = False) -> Iterator[None]:
    try:
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
        yield
        if write:
            db.commit()
    except ClubRequestError:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        if isinstance(exc, IntegrityError):
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            conflicts = {
                "uq_representative_application_club_applicant": DUPLICATE_APPLICATION,
                "uq_one_selected_representative_application_per_club": POSITION_FILLED,
                "uq_representative_application_review_operation": IDEMPOTENCY_CONFLICT,
            }
            if constraint in conflicts:
                raise ClubRequestError(409, conflicts[constraint]) from exc
        logger.error("Representative application operation failed (%s)", type(exc).__name__)
        raise ClubRequestError(503, "Representative applications are temporarily unavailable.") from exc


def _student_school(db: Session, student: Account) -> UUID:
    if (
        student.account_type != AccountType.STUDENT
        or not student.is_active
        or student.school_id is None
        or not db.scalar(select(School.is_active).where(School.id == student.school_id))
    ):
        raise ClubRequestError(403, "Permission denied.")
    return student.school_id


def _admin_school(db: Session, admin: Account, *, lock_assignment: bool = False) -> UUID:
    if admin.account_type != AccountType.SCHOOL_ADMIN or not admin.is_active or admin.school_id is None:
        raise ClubRequestError(403, "Permission denied.")

    query = (
        select(SchoolAdmin.school_id)
        .join(School, School.id == SchoolAdmin.school_id)
        .where(SchoolAdmin.school_id == admin.school_id, SchoolAdmin.account_id == admin.id, School.is_active)
    )
    if lock_assignment:
        # Match club review's order. Rollover and closure acquire this assignment before year locks.
        query = query.with_for_update(read=True, key_share=True, of=SchoolAdmin)

    school_id = db.scalar(query)
    if school_id is None:
        raise ClubRequestError(403, "Permission denied.")
    return school_id


def _require_page_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_REPRESENTATIVE_PAGE_SIZE:
        raise ClubRequestError(422, f"Limit must be between 1 and {MAX_REPRESENTATIVE_PAGE_SIZE}.")


def _selected_applicant_query(school_id, club_id):
    return select(RepresentativeApplication.applicant_account_id).where(
        RepresentativeApplication.school_id == school_id,
        RepresentativeApplication.club_id == club_id,
        RepresentativeApplication.status == RepresentativeApplicationStatus.SELECTED,
    )


def selected_representative_account_id(db: Session, club: Club) -> UUID | None:
    """Resolve the selected student under the caller's club lock."""
    return db.scalar(_selected_applicant_query(club.school_id, club.id))


def _lock_rollover_club(db: Session, school_id: UUID, club_id: UUID) -> tuple[Club, AcademicYear]:
    locked = lock_club_for_year_write(db, school_id, club_id)
    if locked is None or locked[0].rolled_over_from_club_id is None:
        raise ClubRequestError(404, "Carried-forward club not found.")
    return locked


def _active_applicant(db: Session, school_id: UUID, applicant_id: UUID) -> bool:
    return db.scalar(
        select(Account.id).where(
            Account.id == applicant_id,
            Account.school_id == school_id,
            Account.account_type == AccountType.STUDENT,
            Account.is_active,
        ).with_for_update(read=True)
    ) is not None


def list_representative_positions(
    db: Session, student: Account, *, limit: int, after: UUID | None,
) -> RepresentativePage[RepresentativePosition]:
    _require_page_limit(limit)
    with _application_operation(db):
        school_id = _student_school(db, student)
        already_applied = select(RepresentativeApplication.id).where(
            RepresentativeApplication.school_id == school_id,
            RepresentativeApplication.club_id == Club.id,
            RepresentativeApplication.applicant_account_id == student.id,
        ).exists()
        expired = select(AcademicYearClosureRequest.id).where(
            AcademicYearClosureRequest.school_id == school_id,
            AcademicYearClosureRequest.academic_year_id == AcademicYear.id,
            AcademicYearClosureRequest.status.in_((ClosureRequestStatus.PENDING, ClosureRequestStatus.CONFIRMED)),
            or_(
                AcademicYearClosureRequest.completion_deadline_at <= func.statement_timestamp(),
                AcademicYearClosureRequest.finalized_at.is_not(None),
            ),
        ).exists()
        query = (
            select(Club.id.label("club_id"), Club.academic_year_id, Club.name, Club.description, Club.category)
            .join(AcademicYear, and_(
                AcademicYear.id == Club.academic_year_id, AcademicYear.school_id == Club.school_id,
            ))
            .where(
                Club.school_id == school_id,
                AcademicYear.status == AcademicYearStatus.ACTIVE,
                Club.status == ClubStatus.PENDING,
                Club.rolled_over_from_club_id.is_not(None),
                ~_selected_applicant_query(school_id, Club.id).exists(),
                ~already_applied,
                ~expired,
            )
        )
        if after is not None:
            query = query.where(Club.id > after)
        rows = db.execute(query.order_by(Club.id).limit(limit + 1)).mappings().all()
        items = [RepresentativePosition.model_validate(row) for row in rows[:limit]]
        return RepresentativePage[RepresentativePosition](
            items=items, next_cursor=items[-1].club_id if len(rows) > limit else None,
        )


def list_student_representative_applications(
    db: Session, student: Account, *, limit: int, after: UUID | None,
) -> RepresentativePage[RepresentativeApplicationResponse]:
    _require_page_limit(limit)
    with _application_operation(db):
        school_id = _student_school(db, student)
        query = select(RepresentativeApplication).where(
            RepresentativeApplication.school_id == school_id,
            RepresentativeApplication.applicant_account_id == student.id,
        )
        if after is not None:
            query = query.where(RepresentativeApplication.id > after)
        rows = db.scalars(query.order_by(RepresentativeApplication.id).limit(limit + 1)).all()
        items = [RepresentativeApplicationResponse.model_validate(row) for row in rows[:limit]]
        return RepresentativePage[RepresentativeApplicationResponse](
            items=items, next_cursor=items[-1].id if len(rows) > limit else None,
        )


def list_club_representative_applications(
    db: Session, admin: Account, club_id: UUID, *, limit: int, after: UUID | None,
) -> RepresentativePage[RepresentativeApplicantResponse]:
    _require_page_limit(limit)
    with _application_operation(db):
        school_id = _admin_school(db, admin)
        club_exists = db.scalar(select(Club.id).where(
            Club.id == club_id, Club.school_id == school_id, Club.rolled_over_from_club_id.is_not(None),
        ))
        if club_exists is None:
            raise ClubRequestError(404, "Carried-forward club not found.")

        query = (
            select(RepresentativeApplication, Account.full_name)
            .join(Account, and_(
                Account.id == RepresentativeApplication.applicant_account_id,
                Account.school_id == RepresentativeApplication.school_id,
            ))
            .where(RepresentativeApplication.school_id == school_id, RepresentativeApplication.club_id == club_id)
        )
        if after is not None:
            query = query.where(RepresentativeApplication.id > after)
        rows = db.execute(query.order_by(RepresentativeApplication.id).limit(limit + 1)).all()
        items = [
            RepresentativeApplicantResponse(
                **ReviewedRepresentativeApplicationResponse.model_validate(application).model_dump(),
                applicant_name=name,
            )
            for application, name in rows[:limit]
        ]
        return RepresentativePage[RepresentativeApplicantResponse](
            items=items, next_cursor=items[-1].id if len(rows) > limit else None,
        )


def apply_for_representative_position(
    db: Session, student: Account, club_id: UUID, idempotency_key: UUID,
) -> RepresentativeApplicationResponse:
    with _application_operation(db, write=True):
        school_id = _student_school(db, student)
        club, year = _lock_rollover_club(db, school_id, club_id)
        application_id = uuid5(NAMESPACE_URL, f"clubos:representative-application:{student.id}:{club.id}:{idempotency_key}")
        existing = db.scalar(select(RepresentativeApplication).where(
            RepresentativeApplication.school_id == school_id,
            RepresentativeApplication.club_id == club.id,
            RepresentativeApplication.applicant_account_id == student.id,
        ).execution_options(populate_existing=True))
        if existing is not None:
            if existing.id != application_id:
                raise ClubRequestError(409, DUPLICATE_APPLICATION)
            return RepresentativeApplicationResponse.model_validate(existing)

        if year.status != AcademicYearStatus.ACTIVE:
            raise ClubRequestError(409, "Academic year is not active.")
        if club.status != ClubStatus.PENDING or selected_representative_account_id(db, club) is not None:
            raise ClubRequestError(409, "This club is not accepting representative applications.")
        if not _active_applicant(db, school_id, student.id):
            raise ClubRequestError(403, "Permission denied.")
        submitted_at = require_completion_window_open(db, year, ClubRequestError)

        application = RepresentativeApplication(
            id=application_id,
            school_id=school_id,
            club_id=club.id,
            applicant_account_id=student.id,
            status=RepresentativeApplicationStatus.PENDING,
            submitted_at=submitted_at,
        )
        db.add_all([application, AuditLog(
            actor_account_id=student.id,
            action=APPLICATION_SUBMITTED,
            entity_type=APPLICATION_ENTITY_TYPE,
            entity_id=str(application_id),
            details={"club_id": str(club.id)},
            occurred_at=submitted_at,
        )])
        return RepresentativeApplicationResponse.model_validate(application)


def review_representative_application(
    db: Session, admin: Account, club_id: UUID, application_id: UUID,
    data: RepresentativeApplicationReviewRequest, idempotency_key: UUID,
) -> ReviewedRepresentativeApplicationResponse:
    with _application_operation(db, write=True):
        school_id = _admin_school(db, admin, lock_assignment=True)
        club, year = _lock_rollover_club(db, school_id, club_id)
        application = db.scalar(select(RepresentativeApplication).where(
            RepresentativeApplication.id == application_id,
            RepresentativeApplication.school_id == school_id,
            RepresentativeApplication.club_id == club.id,
        ).execution_options(populate_existing=True))
        if application is None:
            raise ClubRequestError(404, "Representative application not found.")

        operation_id = uuid5(NAMESPACE_URL, f"clubos:representative-application-review:{admin.id}:{club.id}:{idempotency_key}")
        if application.review_operation_id == operation_id:
            if application.status != data.decision or application.review_comments != data.comments:
                raise ClubRequestError(409, IDEMPOTENCY_CONFLICT)
            return ReviewedRepresentativeApplicationResponse.model_validate(application)
        claimed = db.scalar(select(RepresentativeApplication.id).where(
            RepresentativeApplication.school_id == school_id,
            RepresentativeApplication.club_id == club.id,
            RepresentativeApplication.review_operation_id == operation_id,
        ))
        if claimed is not None:
            raise ClubRequestError(409, IDEMPOTENCY_CONFLICT)
        if year.status != AcademicYearStatus.ACTIVE:
            raise ClubRequestError(409, "Academic year is not active.")
        if application.status != RepresentativeApplicationStatus.PENDING:
            raise ClubRequestError(409, "This application has already been reviewed.")
        if data.decision == RepresentativeApplicationStatus.SELECTED:
            if club.status != ClubStatus.PENDING or selected_representative_account_id(db, club) is not None:
                raise ClubRequestError(409, POSITION_FILLED)
            if not _active_applicant(db, school_id, application.applicant_account_id):
                raise ClubRequestError(409, "The applicant is no longer an eligible student.")
        reviewed_at = require_completion_window_open(db, year, ClubRequestError)

        application.status = data.decision
        application.reviewed_by_account_id = admin.id
        application.reviewed_at = reviewed_at
        application.review_comments = data.comments
        application.review_operation_id = operation_id
        db.add(AuditLog(
            id=operation_id,
            actor_account_id=admin.id,
            action=APPLICATION_REVIEWED,
            entity_type=APPLICATION_ENTITY_TYPE,
            entity_id=str(application.id),
            details={"club_id": str(club.id), "decision": data.decision.value},
            occurred_at=reviewed_at,
        ))
        return ReviewedRepresentativeApplicationResponse.model_validate(application)
