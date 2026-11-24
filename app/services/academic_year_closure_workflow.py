import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.academic_year import AcademicYear
from app.models.academic_year_closure_request import AcademicYearClosureRequest
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.enums import AcademicYearStatus, AccountType, ClosureRequestStatus
from app.models.school import School
from app.schemas.academic_year import ClosureRequestResponse, ClosureTeacherResponse
from app.services.academic_year_closure import completion_window_status, count_pending_reviews
from app.services.academic_year_closure_finalization import finalize_locked_closure_window
from app.services.academic_year_write_guard import lock_academic_year_for_write
from app.services.academic_years import AcademicYearError, require_admin_school


logger = logging.getLogger(__name__)
COMPLETION_WINDOW = timedelta(hours=168)


@contextmanager
def _closure_operation(db: Session, *, write: bool = False) -> Iterator[None]:
    try:
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
        yield
        if write:
            db.commit()
    except AcademicYearError:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Academic-year closure workflow failed (%s)", type(exc).__name__)
        raise AcademicYearError(503, "Academic-year closure is temporarily unavailable.") from exc


def _lock_active_teacher(db: Session, school_id: UUID, teacher_id: UUID) -> UUID | None:
    return db.scalar(
        select(Account.id)
        .where(
            Account.id == teacher_id,
            Account.school_id == school_id,
            Account.account_type == AccountType.TEACHER,
            Account.is_active.is_(True),
        )
        .with_for_update(read=True)
    )


def get_closure_teacher(db: Session, admin: Account) -> ClosureTeacherResponse:
    with _closure_operation(db):
        school_id = require_admin_school(db, admin)
        teacher_id = db.scalar(select(School.closure_teacher_account_id).where(School.id == school_id))
        return ClosureTeacherResponse(teacher_account_id=teacher_id)


def set_closure_teacher(db: Session, admin: Account, teacher_id: UUID) -> ClosureTeacherResponse:
    with _closure_operation(db, write=True):
        school_id = require_admin_school(db, admin, for_update=True)
        if _lock_active_teacher(db, school_id, teacher_id) is None:
            raise AcademicYearError(404, "Active teacher not found.")

        school = db.get(School, school_id, populate_existing=True)
        previous_teacher = school.closure_teacher_account_id
        if previous_teacher != teacher_id:
            school.closure_teacher_account_id = teacher_id
            school.closure_teacher_designation_id = uuid4()
            db.add(AuditLog(
                actor_account_id=admin.id,
                action="CLOSURE_TEACHER_DESIGNATED",
                entity_type="school",
                entity_id=str(school_id),
                details={
                    "previous_teacher_account_id": str(previous_teacher) if previous_teacher else None,
                    "teacher_account_id": str(teacher_id),
                },
            ))
        return ClosureTeacherResponse(teacher_account_id=teacher_id)


def _lock_year(db: Session, school_id: UUID, year_id: UUID) -> AcademicYear:
    year = lock_academic_year_for_write(db, school_id, year_id, exclusive=True)
    if year is None:
        raise AcademicYearError(404, "Academic year not found.")
    return year


def _require_no_pending_reviews(db: Session, school_id: UUID, year_id: UUID) -> None:
    count = count_pending_reviews(db, school_id, year_id)
    if count:
        raise AcademicYearError(409, {
            "message": "Pending activity revisions block closure.",
            "pending_review_blocker_count": count,
        })


def _year_requests(school_id: UUID, year_id: UUID):
    return select(AcademicYearClosureRequest).where(
        AcademicYearClosureRequest.school_id == school_id,
        AcademicYearClosureRequest.academic_year_id == year_id,
        AcademicYearClosureRequest.status.in_((ClosureRequestStatus.PENDING, ClosureRequestStatus.CONFIRMED)),
    )


def _database_now(db: Session) -> datetime:
    return db.execute(select(func.clock_timestamp())).scalar_one().astimezone(timezone.utc)


def _request_response(request: AcademicYearClosureRequest, as_of: datetime) -> ClosureRequestResponse:
    return ClosureRequestResponse(
        id=request.id,
        academic_year_id=request.academic_year_id,
        teacher_account_id=request.teacher_account_id,
        requested_at=request.requested_at,
        completion_deadline_at=request.completion_deadline_at,
        finalized_at=request.finalized_at,
        window_status=completion_window_status(request.status, request.completion_deadline_at, request.finalized_at, as_of),
        status=request.status,
        confirmed_by_account_id=request.confirmed_by_account_id,
        confirmed_at=request.confirmed_at,
    )


def _duplicate_request(request: AcademicYearClosureRequest, teacher_id: UUID) -> AcademicYearError:
    message = "An academic-year closure request already exists for this year."
    if request.teacher_account_id == teacher_id:
        message = "An academic-year closure request has already been submitted by this teacher."
    return AcademicYearError(409, {
        "code": "ACADEMIC_YEAR_CLOSURE_REQUEST_EXISTS",
        "message": message,
        "request_id": str(request.id),
        "completion_deadline_at": request.completion_deadline_at.astimezone(timezone.utc).isoformat(),
    })


def get_closure_request(db: Session, admin: Account, year_id: UUID) -> ClosureRequestResponse | None:
    with _closure_operation(db):
        school_id = require_admin_school(db, admin)
        if db.scalar(select(AcademicYear.id).where(
            AcademicYear.id == year_id, AcademicYear.school_id == school_id,
        )) is None:
            raise AcademicYearError(404, "Academic year not found.")
        request = db.scalar(_year_requests(school_id, year_id))
        return _request_response(request, _database_now(db)) if request is not None else None


def request_year_closure(
    db: Session, teacher: Account, year_id: UUID, idempotency_key: UUID,
) -> ClosureRequestResponse:
    with _closure_operation(db, write=True):
        if teacher.account_type != AccountType.TEACHER or not teacher.is_active or teacher.school_id is None:
            raise AcademicYearError(403, "Permission denied.")

        school_id, teacher_id = teacher.school_id, teacher.id
        # Lock the designation before the year so the admin cannot replace it mid-request.
        designation_id = db.scalar(
            select(School.closure_teacher_designation_id)
            .where(
                School.id == school_id,
                School.is_active.is_(True),
                School.closure_teacher_account_id == teacher_id,
            )
            .with_for_update(read=True)
        )
        if designation_id is None:
            # A former designee can replay their own request with the original key.
            replay = db.scalar(_year_requests(school_id, year_id).where(
                AcademicYearClosureRequest.teacher_account_id == teacher_id,
                AcademicYearClosureRequest.idempotency_key == idempotency_key,
            ))
            if replay is not None and _lock_active_teacher(db, school_id, teacher_id) is not None:
                return _request_response(replay, _database_now(db))
            raise AcademicYearError(403, "Only the designated closure teacher may request closure.")

        year = _lock_year(db, school_id, year_id)
        if _lock_active_teacher(db, school_id, teacher_id) is None:
            raise AcademicYearError(403, "Permission denied.")

        existing = db.scalar(select(AcademicYearClosureRequest).where(
            AcademicYearClosureRequest.school_id == school_id,
            AcademicYearClosureRequest.idempotency_key == idempotency_key,
        ))
        if existing is not None:
            if existing.academic_year_id != year_id:
                raise AcademicYearError(409, "Idempotency key reused for another closure request.")
            if existing.teacher_account_id != teacher_id:
                raise _duplicate_request(existing, teacher_id)
            return _request_response(existing, _database_now(db))

        current = db.scalar(_year_requests(school_id, year_id))
        if current is not None:
            raise _duplicate_request(current, teacher_id)

        if year.status != AcademicYearStatus.ACTIVE:
            raise AcademicYearError(409, "Academic year is not active.")

        requested_at = _database_now(db)
        request = AcademicYearClosureRequest(
            id=uuid4(), school_id=school_id, academic_year_id=year_id,
            teacher_account_id=teacher_id, designation_id=designation_id,
            idempotency_key=idempotency_key, status=ClosureRequestStatus.PENDING,
            requested_at=requested_at, completion_deadline_at=requested_at + COMPLETION_WINDOW,
        )
        db.add_all([
            request,
            AuditLog(
                actor_account_id=teacher_id,
                action="ACADEMIC_YEAR_CLOSURE_REQUESTED",
                entity_type="academic_year",
                entity_id=str(year_id),
                details={"school_id": str(school_id), "request_id": str(request.id)},
                occurred_at=requested_at,
            ),
        ])
        return _request_response(request, requested_at)


def confirm_year_closure(
    db: Session, admin: Account, year_id: UUID, request_id: UUID,
) -> ClosureRequestResponse:
    with _closure_operation(db, write=True):
        school_id = require_admin_school(db, admin, for_update=True)
        year = _lock_year(db, school_id, year_id)
        request = db.scalar(
            _year_requests(school_id, year_id)
            .where(AcademicYearClosureRequest.id == request_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if request is None:
            raise AcademicYearError(404, "Closure request not found.")

        now = _database_now(db)
        if request.status == ClosureRequestStatus.CONFIRMED and year.status in (
            AcademicYearStatus.CLOSED, AcademicYearStatus.ARCHIVED,
        ):
            return _request_response(request, now)
        if year.status != AcademicYearStatus.ACTIVE or request.status != ClosureRequestStatus.PENDING:
            raise AcademicYearError(409, "Closure request cannot be confirmed in its current state.")

        if request.completion_deadline_at > now:
            raise AcademicYearError(409, {
                "code": "ACADEMIC_YEAR_COMPLETION_WINDOW_OPEN",
                "message": "The academic-year completion window is still open.",
                "completion_deadline_at": request.completion_deadline_at.astimezone(timezone.utc).isoformat(),
            })

        # Finalize the original authorized request, even if its teacher was replaced later.
        finalize_locked_closure_window(db, year, request)
        _require_no_pending_reviews(db, school_id, year_id)
        if request.finalized_at is None:
            raise AcademicYearError(409, "The academic-year completion window could not be finalized.")

        year.status = AcademicYearStatus.CLOSED
        request.status = ClosureRequestStatus.CONFIRMED
        request.confirmed_by_account_id = admin.id
        request.confirmed_at = _database_now(db)
        db.add(AuditLog(
            actor_account_id=admin.id,
            action="ACADEMIC_YEAR_CLOSED",
            entity_type="academic_year",
            entity_id=str(year_id),
            details={"school_id": str(school_id), "request_id": str(request_id)},
            occurred_at=request.confirmed_at,
        ))
        return _request_response(request, request.confirmed_at)
