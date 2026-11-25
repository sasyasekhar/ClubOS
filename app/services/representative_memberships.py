
from datetime import datetime, timezone
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.school import School
from app.models.school_admin import SchoolAdmin
from app.models.club_membership import ClubMembership
from app.models.audit_log import AuditLog
from app.models.enums import (
    AccountType,
    AcademicYearStatus,
    ClubRole,
    ClubStatus,
    MembershipStatus,
)
from app.services.academic_year_write_guard import lock_club_for_year_write, require_completion_window_open
from app.services.club_registration import ClubRequestError
from app.services.operation_receipts import find_receipt, stage_receipt
from app.services.representative_applications import selected_representative_account_id


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

    locked = lock_club_for_year_write(db, admin.school_id, club_id)
    if locked is None:
        raise ClubRequestError(404, "Club not found.")

    return locked


def end_representative(
    db: Session,
    admin: Account,
    club_id: UUID,
    membership_id: UUID,
    key: UUID,
    reason: str | None,
):
    club, year = lock_admin_club(db, admin, club_id)

    membership = db.get(ClubMembership, membership_id)

    if membership is None or membership.club_id != club.id:
        raise ClubRequestError(404, "Membership not found.")

    event_id = uuid5(
        NAMESPACE_URL,
        f"clubos:membership-end:{admin.id}:{membership_id}:{key}",
    )

    action = "REPRESENTATIVE_ENDED"
    if find_receipt(db, event_id, action, reason, ClubRequestError) is not None:
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

    if year.status != AcademicYearStatus.ACTIVE:
        raise ClubRequestError(409, "Academic year is not active.")

    try:
        require_completion_window_open(db, year, ClubRequestError)
        membership.status = MembershipStatus.ENDED
        membership.end_date = datetime.now(timezone.utc).date()

        stage_receipt(db, event_id, action, reason)
        db.add(AuditLog(
            id=event_id,
            actor_account_id=admin.id,
            action=action,
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
    club, year = lock_admin_club(db, admin, club_id)

    old = db.get(ClubMembership, old_membership_id)

    if old is None or old.club_id != club.id:
        raise ClubRequestError(404, "Membership not found.")

    event_id = uuid5(
        NAMESPACE_URL,
        f"clubos:membership-rejoin:{admin.id}:{old.id}:{key}",
    )

    action = "REPRESENTATIVE_REJOINED"
    existing = find_receipt(db, event_id, action, None, ClubRequestError)

    if existing is not None:
        membership = db.get(ClubMembership, existing.result_id) if existing.result_id else None
        if membership is None or membership.club_id != club.id:
            raise ClubRequestError(409, "Membership unavailable.")

        return membership_result(membership)

    if (
        club.status != ClubStatus.ACTIVE
        or old.status != MembershipStatus.ENDED
    ):
        raise ClubRequestError(409, "Membership cannot rejoin.")

    if year.status != AcademicYearStatus.ACTIVE:
        raise ClubRequestError(409, "Academic year is not active.")

    account = db.get(Account, old.account_id)

    if (
        account is None
        or not account.is_active
        or account.school_id != club.school_id
    ):
        raise ClubRequestError(409, "Account is unavailable.")

    if old.role == ClubRole.STUDENT_REP:
        student_id = club.created_by
        if club.rolled_over_from_club_id is not None:
            student_id = selected_representative_account_id(db, club)
        if account.account_type != AccountType.STUDENT or account.id != student_id:
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
        require_completion_window_open(db, year, ClubRequestError)
        db.add(new_membership)
        stage_receipt(db, event_id, action, None, result_id=new_id)
        db.add(AuditLog(
            id=event_id,
            actor_account_id=admin.id,
            action=action,
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
