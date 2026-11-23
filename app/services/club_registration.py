
from datetime import date
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.school import School
from app.models.enums import (
    AccountType,
    ClubStatus,
    ClubRole,
    MembershipStatus,
)
from app.schemas.club import ClubRegistrationRequest
from app.services.academic_year_write_guard import lock_academic_year_for_write


class ClubRequestError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def response_for(club: Club, teacher_id: UUID):
    return {
        "id": club.id,
        "school_id": club.school_id,
        "academic_year_id": club.academic_year_id,
        "status": club.status,
        "teacher_account_id": teacher_id,
    }


def existing_request(db, club, student, data):
    original_teacher = db.scalar(
        select(ClubMembership.account_id)
        .where(
            ClubMembership.club_id == club.id,
            ClubMembership.role == ClubRole.TEACHER_REP,
        )
        .order_by(
            ClubMembership.created_at.asc(),
            ClubMembership.id.asc(),
        )
        .limit(1)
    )

    if (
        club.created_by != student.id
        or club.school_id != student.school_id
        or club.name != data.name
        or club.description != data.description
        or club.category != data.category
        or original_teacher != data.teacher_account_id
    ):
        raise ClubRequestError(
            409, "Idempotency key was used for another request."
        )

    return response_for(club, original_teacher)


def register_club(
    db: Session,
    student: Account,
    data: ClubRegistrationRequest,
    idempotency_key: UUID,
):
    if (
        student.account_type != AccountType.STUDENT
        or not student.is_active
        or student.school_id is None
    ):
        raise ClubRequestError(403, "Permission denied.")

    # A stable ID prevents duplicate inserts for the same request.
    club_id = uuid5(
        NAMESPACE_URL,
        f"clubos:club-registration:{student.id}:{idempotency_key}",
    )

    existing_query = select(Club).where(Club.id == club_id, Club.school_id == student.school_id)
    existing = db.scalar(existing_query)
    if existing is not None:
        return existing_request(db, existing, student, data)

    # Take the club's school foreign-key lock before the year lock.
    school_active = db.scalar(
        select(School.is_active)
        .where(School.id == student.school_id)
        .with_for_update(read=True, key_share=True, of=School)
    )
    if not school_active:
        raise ClubRequestError(403, "School is inactive.")

    year = lock_academic_year_for_write(db, student.school_id)
    if year is None:
        # A concurrent request may have committed before the year closed.
        existing = db.scalar(existing_query)
        if existing is not None:
            return existing_request(db, existing, student, data)
        raise ClubRequestError(
            409, "An active academic year is required."
        )

    teacher = db.scalar(
        select(Account).where(
            Account.id == data.teacher_account_id,
            Account.school_id == student.school_id,
            Account.account_type == AccountType.TEACHER,
            Account.is_active.is_(True),
        )
    )
    if teacher is None:
        raise ClubRequestError(
            404, "Selected teacher is not available."
        )

    club = Club(
        id=club_id,
        school_id=student.school_id,
        academic_year_id=year.id,
        created_by=student.id,
        name=data.name,
        description=data.description,
        category=data.category,
        status=ClubStatus.PENDING,
    )

    # Teacher selection is pending until the later
    # representative invitation/acceptance workflow.
    membership = ClubMembership(
        club_id=club_id,
        account_id=teacher.id,
        role=ClubRole.TEACHER_REP,
        status=MembershipStatus.PENDING,
        start_date=date.today(),
    )

    audit = AuditLog(
        actor_account_id=student.id,
        action="CLUB_REGISTRATION_REQUESTED",
        entity_type="club",
        entity_id=str(club_id),
        details={"teacher_account_id": str(teacher.id)},
    )

    try:
        db.add_all([club, membership, audit])
        db.commit()
    except IntegrityError as exc:
        db.rollback()

        # Another request may have committed the same ID.
        existing = db.scalar(existing_query)
        if existing is not None:
            return existing_request(db, existing, student, data)

        raise ClubRequestError(
            409, "Club request could not be completed."
        ) from exc
    except Exception:
        db.rollback()
        raise

    return response_for(club, teacher.id)
