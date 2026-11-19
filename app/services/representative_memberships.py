
from datetime import datetime, timezone
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.school import School
from app.models.school_admin import SchoolAdmin
from app.models.academic_year import AcademicYear
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.audit_log import AuditLog
from app.models.enums import (
    AccountType,
    AcademicYearStatus,
    ClubRole,
    ClubStatus,
    MembershipStatus,
)
from app.services.club_registration import ClubRequestError


def membership_result(membership):
    return {
        "membership_id": membership.id,
        "club_id": membership.club_id,
        "account_id": membership.account_id,
        "role": membership.role,
        "status": membership.status,
        "start_date": membership.start_date,
        "end_date": membership.end_date,
    }


def lock_admin_club(db: Session, admin: Account, club_id: UUID):
    if (
        admin.account_type != AccountType.SCHOOL_ADMIN
        or not admin.is_active
        or admin.school_id is None
    ):
        raise ClubRequestError(403, "Permission denied.")

    school = db.get(School, admin.school_id)
    admin_link = db.get(SchoolAdmin, admin.school_id)

    if (
        school is None
        or not school.is_active
        or admin_link is None
        or admin_link.account_id != admin.id
    ):
        raise ClubRequestError(403, "Permission denied.")

    club = db.scalar(
        select(Club)
        .where(
            Club.id == club_id,
            Club.school_id == admin.school_id,
        )
        .with_for_update()
    )

    if club is None:
        raise ClubRequestError(404, "Club not found.")

    return club


def end_representative(
    db: Session,
    admin: Account,
    club_id: UUID,
    membership_id: UUID,
    key: UUID,
    reason: str | None,
):
    club = lock_admin_club(db, admin, club_id)

    membership = db.get(ClubMembership, membership_id)

    if membership is None or membership.club_id != club.id:
        raise ClubRequestError(404, "Membership not found.")

    event_id = uuid5(
        NAMESPACE_URL,
        f"clubos:membership-end:{admin.id}:{membership_id}:{key}",
    )

    existing = db.get(AuditLog, event_id)

    if existing is not None:
        if (
            existing.action != "REPRESENTATIVE_ENDED"
            or existing.actor_account_id != admin.id
            or (existing.details or {}).get("reason") != reason
        ):
            raise ClubRequestError(409, "Idempotency key conflict.")

        return membership_result(membership)

    if (
        club.status != ClubStatus.ACTIVE
        or membership.status != MembershipStatus.ACTIVE
    ):
        raise ClubRequestError(409, "Membership cannot be ended.")

    if membership.role not in (
        ClubRole.STUDENT_REP,
        ClubRole.TEACHER_REP,
    ):
        raise ClubRequestError(403, "Invalid representative role.")

    try:
        membership.status = MembershipStatus.ENDED
        membership.end_date = datetime.now(timezone.utc).date()

        db.add(AuditLog(
            id=event_id,
            actor_account_id=admin.id,
            action="REPRESENTATIVE_ENDED",
            entity_type="club_membership",
            entity_id=str(membership.id),
            details={
                "club_id": str(club.id),
                "reason": reason,
            },
        ))

        db.commit()

    except Exception:
        db.rollback()
        raise

    return membership_result(membership)


def rejoin_representative(
    db: Session,
    admin: Account,
    club_id: UUID,
    old_membership_id: UUID,
    key: UUID,
):
    club = lock_admin_club(db, admin, club_id)

    old = db.get(ClubMembership, old_membership_id)

    if old is None or old.club_id != club.id:
        raise ClubRequestError(404, "Membership not found.")

    event_id = uuid5(
        NAMESPACE_URL,
        f"clubos:membership-rejoin:{admin.id}:{old.id}:{key}",
    )

    existing = db.get(AuditLog, event_id)

    if existing is not None:
        new_id = UUID(existing.details["new_membership_id"])
        membership = db.get(ClubMembership, new_id)

        if membership is None:
            raise ClubRequestError(409, "Membership unavailable.")

        return membership_result(membership)

    if (
        club.status != ClubStatus.ACTIVE
        or old.status != MembershipStatus.ENDED
    ):
        raise ClubRequestError(409, "Membership cannot rejoin.")

    year = db.get(AcademicYear, club.academic_year_id)
    if (
        year is None
        or year.school_id != club.school_id
        or year.status != AcademicYearStatus.ACTIVE
    ):
        raise ClubRequestError(409, "Academic year is not active.")

    account = db.get(Account, old.account_id)

    if (
        account is None
        or not account.is_active
        or account.school_id != club.school_id
    ):
        raise ClubRequestError(409, "Account is unavailable.")

    if old.role == ClubRole.STUDENT_REP:
        if (
            account.account_type != AccountType.STUDENT
            or account.id != club.created_by
        ):
            raise ClubRequestError(409, "Student is not eligible.")
        new_status = MembershipStatus.ACTIVE

    elif old.role == ClubRole.TEACHER_REP:
        if account.account_type != AccountType.TEACHER:
            raise ClubRequestError(409, "Teacher is not eligible.")
        new_status = MembershipStatus.PENDING

    else:
        raise ClubRequestError(409, "Invalid representative role.")

    current = db.scalar(
        select(ClubMembership.id).where(
            ClubMembership.club_id == club.id,
            ClubMembership.role == old.role,
            ClubMembership.status.in_([
                MembershipStatus.ACTIVE,
                MembershipStatus.PENDING,
            ]),
        )
    )

    if current is not None:
        raise ClubRequestError(409, "Current representative exists.")

    new_id = uuid5(
        NAMESPACE_URL,
        f"clubos:rejoined-membership:{admin.id}:{old.id}:{key}",
    )

    new_membership = ClubMembership(
        id=new_id,
        club_id=club.id,
        account_id=account.id,
        role=old.role,
        status=new_status,
        start_date=datetime.now(timezone.utc).date(),
    )

    try:
        db.add(new_membership)
        db.add(AuditLog(
            id=event_id,
            actor_account_id=admin.id,
            action="REPRESENTATIVE_REJOINED",
            entity_type="club_membership",
            entity_id=str(new_id),
            details={
                "club_id": str(club.id),
                "previous_membership_id": str(old.id),
                "new_membership_id": str(new_id),
            },
        ))

        db.commit()

    except IntegrityError as exc:
        db.rollback()
        raise ClubRequestError(
            409, "Representative already assigned."
        ) from exc

    except Exception:
        db.rollback()
        raise

    return membership_result(new_membership)
