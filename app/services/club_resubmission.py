
from datetime import date
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.school import School
from app.models.club_membership import ClubMembership
from app.models.audit_log import AuditLog
from app.models.enums import (
    AccountType,
    AcademicYearStatus,
    ClubStatus,
    ClubRole,
    MembershipStatus,
)
from app.schemas.club import ClubRegistrationRequest
from app.services.academic_year_write_guard import lock_club_for_year_write, require_completion_window_open
from app.services.club_registration import ClubRequestError
from app.services.operation_receipts import find_receipt, stage_receipt


def resubmit_club(
    db: Session,
    student: Account,
    club_id: UUID,
    data: ClubRegistrationRequest,
    idempotency_key: UUID,
):
    if (
        student.account_type != AccountType.STUDENT
        or not student.is_active
        or student.school_id is None
    ):
        raise ClubRequestError(403, "Permission denied.")

    locked = lock_club_for_year_write(
        db, student.school_id, club_id, creator_id=student.id
    )
    if locked is None:
        raise ClubRequestError(404, "Club not found.")
    club, year = locked

    event_id = uuid5(
        NAMESPACE_URL,
        f"clubos:club-resubmission:{student.id}:{club_id}:{idempotency_key}",
    )
    submitted_payload = data.model_dump(mode="json")

    action = "CLUB_RESUBMITTED"
    if find_receipt(db, event_id, action, submitted_payload, ClubRequestError) is not None:
        return {
            "id": club.id,
            "school_id": club.school_id,
            "academic_year_id": club.academic_year_id,
            "status": ClubStatus.PENDING,
            "teacher_account_id": data.teacher_account_id,
        }

    if club.status != ClubStatus.CHANGES_REQUESTED:
        raise ClubRequestError(
            409, "Club is not awaiting corrections."
        )

    school = db.get(School, student.school_id)
    if (
        school is None
        or not school.is_active
        or year.status != AcademicYearStatus.ACTIVE
    ):
        raise ClubRequestError(
            409, "Club cannot be resubmitted in its current state."
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
        raise ClubRequestError(404, "Selected teacher is not available.")

    current_teacher = db.scalar(
        select(ClubMembership).where(
            ClubMembership.club_id == club.id,
            ClubMembership.role == ClubRole.TEACHER_REP,
            ClubMembership.status.in_([
                MembershipStatus.PENDING,
                MembershipStatus.ACTIVE,
            ]),
        ).with_for_update()
    )

    try:
        require_completion_window_open(db, year, ClubRequestError)
        if (
            current_teacher is None
            or current_teacher.account_id != teacher.id
        ):
            if current_teacher is not None:
                current_teacher.status = MembershipStatus.ENDED
                current_teacher.end_date = date.today()
                db.flush()

            db.add(ClubMembership(
                club_id=club.id,
                account_id=teacher.id,
                role=ClubRole.TEACHER_REP,
                status=MembershipStatus.PENDING,
                start_date=date.today(),
            ))

        club.name = data.name
        club.description = data.description
        club.category = data.category
        club.status = ClubStatus.PENDING

        stage_receipt(db, event_id, action, submitted_payload)
        db.add(AuditLog(
            id=event_id,
            actor_account_id=student.id,
            action=action,
            entity_type="club",
            entity_id=str(club.id),
        ))

        db.commit()

    except IntegrityError as exc:
        db.rollback()
        raise ClubRequestError(
            409, "Club resubmission could not be completed."
        ) from exc

    except Exception:
        db.rollback()
        raise

    return {
        "id": club.id,
        "school_id": club.school_id,
        "academic_year_id": club.academic_year_id,
        "status": club.status,
        "teacher_account_id": teacher.id,
    }
