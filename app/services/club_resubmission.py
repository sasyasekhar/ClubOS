
from datetime import date
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.school import School
from app.models.academic_year import AcademicYear
from app.models.club import Club
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
from app.services.club_registration import ClubRequestError


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

    # Serializes updates and reviews of the same club.
    club = db.scalar(
        select(Club).where(
            Club.id == club_id,
            Club.school_id == student.school_id,
            Club.created_by == student.id,
        ).with_for_update()
    )

    if club is None:
        raise ClubRequestError(404, "Club not found.")

    event_id = uuid5(
        NAMESPACE_URL,
        f"clubos:club-resubmission:{student.id}:{club_id}:{idempotency_key}",
    )
    submitted_payload = data.model_dump(mode="json")

    existing_event = db.get(AuditLog, event_id)

    if existing_event is not None:
        if (
            existing_event.action != "CLUB_RESUBMITTED"
            or existing_event.actor_account_id != student.id
            or existing_event.entity_id != str(club_id)
            or existing_event.details.get("payload") != submitted_payload
        ):
            raise ClubRequestError(
                409, "Idempotency key reused with different data."
            )

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
    year = db.get(AcademicYear, club.academic_year_id)

    if (
        school is None
        or not school.is_active
        or year is None
        or year.school_id != student.school_id
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

    previous_details = {
        "name": club.name,
        "description": club.description,
        "category": club.category,
        "teacher_account_id": (
            str(current_teacher.account_id)
            if current_teacher else None
        ),
    }

    try:
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

        db.add(AuditLog(
            id=event_id,
            actor_account_id=student.id,
            action="CLUB_RESUBMITTED",
            entity_type="club",
            entity_id=str(club.id),
            details={
                "previous": previous_details,
                "payload": submitted_payload,
            },
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
