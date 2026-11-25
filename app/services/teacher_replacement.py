import logging
from datetime import date
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.academic_year import AcademicYear
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.enums import (
    AccountType, AcademicYearStatus,
    ClubRole, ClubStatus, MembershipStatus,
)
from app.models.school import School
from app.schemas.teacher_invitation import EligibleTeacher, EligibleTeachersPage
from app.services.academic_year_write_guard import lock_club_for_year_write, require_completion_window_open
from app.services.club_registration import ClubRequestError
from app.services.operation_receipts import find_receipt
from app.services.representative_applications import selected_representative_account_id


logger = logging.getLogger(__name__)


def replace_declined_teacher(
    db: Session,
    student: Account,
    club_id: UUID,
    new_teacher_id: UUID,
    idempotency_key: UUID,
):
    return _request_teacher_invitation(db, student, club_id, new_teacher_id, idempotency_key, initial=False)


def select_rollover_teacher(
    db: Session,
    student: Account,
    club_id: UUID,
    teacher_id: UUID,
    idempotency_key: UUID,
):
    return _request_teacher_invitation(db, student, club_id, teacher_id, idempotency_key, initial=True)


def list_eligible_teachers(
    db: Session, student: Account, club_id: UUID, *, limit: int, after: UUID | None,
) -> EligibleTeachersPage:
    _require_student(student)
    try:
        row = db.execute(select(Club, AcademicYear).join(
            AcademicYear, and_(AcademicYear.id == Club.academic_year_id, AcademicYear.school_id == Club.school_id),
        ).where(Club.id == club_id, Club.school_id == student.school_id)).one_or_none()
        if row is None:
            raise ClubRequestError(404, "Club not found.")
        club, year = row
        _authorize_selection(db, student, club, initial=True)
        if club.status != ClubStatus.PENDING or year.status != AcademicYearStatus.ACTIVE:
            raise ClubRequestError(409, "Club is not currently eligible.")
        # Read-only eligibility check. The invitation operation checks again
        # under the write locks before creating a membership.
        require_completion_window_open(db, year, ClubRequestError)

        query = select(Account.id.label("teacher_account_id"), Account.full_name).where(
            Account.school_id == club.school_id,
            Account.account_type == AccountType.TEACHER,
            Account.is_active.is_(True),
        )
        if after is not None:
            query = query.where(Account.id > after)
        rows = db.execute(query.order_by(Account.id).limit(limit + 1)).mappings().all()
        items = [EligibleTeacher.model_validate(row) for row in rows[:limit]]
        return EligibleTeachersPage(
            items=items, next_cursor=items[-1].teacher_account_id if len(rows) > limit else None,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Teacher directory lookup failed (%s)", type(exc).__name__)
        raise ClubRequestError(503, "The teacher directory is temporarily unavailable.") from exc


def _require_student(student: Account) -> None:
    if student.account_type != AccountType.STUDENT or not student.is_active or student.school_id is None:
        raise ClubRequestError(403, "Permission denied.")


def _authorize_selection(db: Session, student: Account, club: Club, *, initial: bool) -> None:
    if initial or (club.rolled_over_from_club_id is not None and club.status != ClubStatus.ACTIVE):
        if club.rolled_over_from_club_id is None or selected_representative_account_id(db, club) != student.id:
            raise ClubRequestError(404, "Club not found.")
    elif club.status == ClubStatus.ACTIVE:
        is_rep = db.scalar(select(ClubMembership.id).where(
            ClubMembership.club_id == club.id,
            ClubMembership.account_id == student.id,
            ClubMembership.role == ClubRole.STUDENT_REP,
            ClubMembership.status == MembershipStatus.ACTIVE,
        ))
        if is_rep is None:
            raise ClubRequestError(403, "Permission denied.")
    elif club.created_by != student.id:
        raise ClubRequestError(404, "Club not found.")


def _invitation_response(membership: ClubMembership):
    return {
        "club_id": membership.club_id,
        "membership_id": membership.id,
        "teacher_account_id": membership.account_id,
        "status": membership.status,
    }


def _legacy_replacement(db: Session, event_id: UUID, teacher_id: UUID) -> ClubMembership | None:
    # Older replacements used random membership IDs and recorded their
    # idempotency key only in the audit event.
    receipt = find_receipt(db, event_id, "TEACHER_REPLACEMENT_REQUESTED", str(teacher_id), ClubRequestError)
    if receipt is None:
        return None
    membership = db.get(ClubMembership, receipt.result_id) if receipt.result_id else None
    if membership is None:
        raise ClubRequestError(409, "Invitation unavailable.")
    return membership


def _check_invitation_history(db: Session, club_id: UUID, *, initial: bool) -> None:
    history = select(ClubMembership.id).where(
        ClubMembership.club_id == club_id, ClubMembership.role == ClubRole.TEACHER_REP,
    )
    if initial:
        if db.scalar(history.limit(1)) is not None:
            raise ClubRequestError(409, "A teacher has already been invited. Replace a declined invitation instead.")
        return

    current = db.scalar(history.where(ClubMembership.status.in_(
        (MembershipStatus.PENDING, MembershipStatus.ACTIVE),
    )))
    if current is not None:
        raise ClubRequestError(409, "A current teacher invitation exists.")
    if db.scalar(history.where(ClubMembership.status == MembershipStatus.DECLINED).limit(1)) is None:
        raise ClubRequestError(409, "No declined invitation found.")


def _request_teacher_invitation(
    db: Session,
    student: Account,
    club_id: UUID,
    new_teacher_id: UUID,
    idempotency_key: UUID,
    *,
    initial: bool,
):
    _require_student(student)
    try:
        locked = lock_club_for_year_write(db, student.school_id, club_id)
        if locked is None:
            raise ClubRequestError(404, "Club not found.")
        club, year = locked
        _authorize_selection(db, student, club, initial=initial)

        action = "TEACHER_INVITATION_REQUESTED" if initial else "TEACHER_REPLACEMENT_REQUESTED"
        operation = "teacher-selection" if initial else "teacher-replacement"
        event_id = uuid5(NAMESPACE_URL, f"clubos:{operation}:{student.id}:{club_id}:{idempotency_key}")
        membership_id = uuid5(NAMESPACE_URL, f"clubos:teacher-invitation:{event_id}")
        existing = db.get(ClubMembership, membership_id)
        if existing is None and not initial:
            existing = _legacy_replacement(db, event_id, new_teacher_id)
        if existing is not None:
            if (
                existing.club_id != club.id
                or existing.account_id != new_teacher_id
                or existing.role != ClubRole.TEACHER_REP
            ):
                raise ClubRequestError(409, "Idempotency key conflict.")
            response = _invitation_response(existing)
            db.rollback()
            return response

        allowed_statuses = (ClubStatus.PENDING,) if initial else (ClubStatus.PENDING, ClubStatus.ACTIVE)
        if club.status not in allowed_statuses:
            raise ClubRequestError(409, "Club cannot change teachers.")

        school_active = db.scalar(select(School.is_active).where(School.id == club.school_id))
        if not school_active or year.status != AcademicYearStatus.ACTIVE:
            raise ClubRequestError(409, "Club is not currently eligible.")

        if club.rolled_over_from_club_id is not None:
            active_student = db.scalar(select(Account.id).where(
                Account.id == student.id,
                Account.school_id == club.school_id,
                Account.account_type == AccountType.STUDENT,
                Account.is_active.is_(True),
            ).with_for_update(read=True))
            if active_student is None:
                raise ClubRequestError(403, "Permission denied.")

        _check_invitation_history(db, club.id, initial=initial)
        teacher_id = db.scalar(
            select(Account.id).where(
                Account.id == new_teacher_id,
                Account.school_id == club.school_id,
                Account.account_type == AccountType.TEACHER,
                Account.is_active.is_(True),
            ).with_for_update(read=True)
        )
        if teacher_id is None:
            raise ClubRequestError(404, "Teacher not found.")

        require_completion_window_open(db, year, ClubRequestError)
        membership = ClubMembership(
            id=membership_id,
            club_id=club.id,
            account_id=teacher_id,
            role=ClubRole.TEACHER_REP,
            status=MembershipStatus.PENDING,
            start_date=date.today(),
        )
        db.add_all([membership, AuditLog(
            id=event_id,
            actor_account_id=student.id,
            action=action,
            entity_type="club_membership",
            entity_id=str(membership_id),
            details={
                "club_id": str(club.id),
                "new_teacher_id": str(teacher_id),
                "membership_id": str(membership_id),
            },
        )])
        response = _invitation_response(membership)
        db.commit()
        return response
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Teacher invitation failed (%s)", type(exc).__name__)
        raise ClubRequestError(503, "Teacher invitation is temporarily unavailable.") from exc
    except Exception:
        db.rollback()
        raise
