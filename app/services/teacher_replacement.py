
from datetime import date
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.school import School
from app.models.academic_year import AcademicYear
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.audit_log import AuditLog
from app.models.enums import (
    AccountType, AcademicYearStatus,
    ClubRole, ClubStatus, MembershipStatus,
)
from app.services.club_registration import ClubRequestError


def replace_declined_teacher(
    db: Session,
    student: Account,
    club_id: UUID,
    new_teacher_id: UUID,
    idempotency_key: UUID,
):
    if (
        student.account_type != AccountType.STUDENT
        or not student.is_active
        or student.school_id is None
    ):
        raise ClubRequestError(403, "Permission denied.")

    # Serialize changes for the same club.
    club = db.scalar(
        select(Club).where(
            Club.id == club_id,
            Club.school_id == student.school_id,
        ).with_for_update()
    )

    if club is None:
        raise ClubRequestError(404, "Club not found.")

    if club.status == ClubStatus.ACTIVE:
        is_rep = db.scalar(
            select(ClubMembership.id).where(
                ClubMembership.club_id == club.id,
                ClubMembership.account_id == student.id,
                ClubMembership.role == ClubRole.STUDENT_REP,
                ClubMembership.status == MembershipStatus.ACTIVE,
            )
        )
        if is_rep is None:
            raise ClubRequestError(403, "Permission denied.")
    elif club.created_by != student.id:
        raise ClubRequestError(404, "Club not found.")

    event_id = uuid5(
        NAMESPACE_URL,
        f"clubos:teacher-replacement:{student.id}:{club_id}:{idempotency_key}",
    )

    existing = db.get(AuditLog, event_id)

    if existing is not None:
        details = existing.details or {}
        if (
            existing.action != "TEACHER_REPLACEMENT_REQUESTED"
            or existing.actor_account_id != student.id
            or details.get("new_teacher_id") != str(new_teacher_id)
        ):
            raise ClubRequestError(409, "Idempotency key conflict.")

        membership = db.get(
            ClubMembership, UUID(details["membership_id"])
        )
        if membership is None:
            raise ClubRequestError(409, "Invitation unavailable.")

        return {
            "club_id": club.id,
            "membership_id": membership.id,
            "teacher_account_id": membership.account_id,
            "status": membership.status,
        }

    if club.status not in (ClubStatus.PENDING, ClubStatus.ACTIVE):
        raise ClubRequestError(409, "Club cannot change teachers.")

    school = db.get(School, club.school_id)
    year = db.get(AcademicYear, club.academic_year_id)

    if (
        school is None or not school.is_active
        or year is None
        or year.school_id != club.school_id
        or year.status != AcademicYearStatus.ACTIVE
    ):
        raise ClubRequestError(409, "Club is not currently eligible.")

    current = db.scalar(
        select(ClubMembership.id).where(
            ClubMembership.club_id == club.id,
            ClubMembership.role == ClubRole.TEACHER_REP,
            ClubMembership.status.in_([
                MembershipStatus.PENDING,
                MembershipStatus.ACTIVE,
            ]),
        )
    )
    if current is not None:
        raise ClubRequestError(409, "A current teacher invitation exists.")

    declined = db.scalar(
        select(ClubMembership.id).where(
            ClubMembership.club_id == club.id,
            ClubMembership.role == ClubRole.TEACHER_REP,
            ClubMembership.status == MembershipStatus.DECLINED,
        ).limit(1)
    )
    if declined is None:
        raise ClubRequestError(409, "No declined invitation found.")

    teacher = db.scalar(
        select(Account).where(
            Account.id == new_teacher_id,
            Account.school_id == club.school_id,
            Account.account_type == AccountType.TEACHER,
            Account.is_active.is_(True),
        )
    )
    if teacher is None:
        raise ClubRequestError(404, "Teacher not found.")

    membership = ClubMembership(
        id=uuid4(),
        club_id=club.id,
        account_id=teacher.id,
        role=ClubRole.TEACHER_REP,
        status=MembershipStatus.PENDING,
        start_date=date.today(),
    )

    audit = AuditLog(
        id=event_id,
        actor_account_id=student.id,
        action="TEACHER_REPLACEMENT_REQUESTED",
        entity_type="club_membership",
        entity_id=str(membership.id),
        details={
            "club_id": str(club.id),
            "new_teacher_id": str(teacher.id),
            "membership_id": str(membership.id),
        },
    )

    try:
        db.add_all([membership, audit])
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "club_id": club.id,
        "membership_id": membership.id,
        "teacher_account_id": teacher.id,
        "status": membership.status,
    }

