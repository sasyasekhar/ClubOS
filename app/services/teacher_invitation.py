
import logging
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.audit_log import AuditLog
from app.models.enums import (
    AcademicYearStatus,
    AccountType,
    ClubRole,
    ClubStatus,
    MembershipStatus,
)
from app.services.academic_year_write_guard import lock_club_for_year_write, require_completion_window_open
from app.services.club_registration import ClubRequestError


logger = logging.getLogger(__name__)


def check_teacher(teacher: Account):
    if (
        teacher.account_type != AccountType.TEACHER
        or not teacher.is_active
        or teacher.school_id is None
    ):
        raise ClubRequestError(403, "Permission denied.")


def list_teacher_invitations(db: Session, teacher: Account):
    check_teacher(teacher)

    rows = db.execute(
        select(ClubMembership, Club)
        .join(Club, ClubMembership.club_id == Club.id)
        .where(
            ClubMembership.account_id == teacher.id,
            ClubMembership.role == ClubRole.TEACHER_REP,
            ClubMembership.status == MembershipStatus.PENDING,
            Club.school_id == teacher.school_id,
            Club.status.notin_([
                ClubStatus.REJECTED,
                ClubStatus.ARCHIVED,
            ]),
        )
    ).all()

    return [
        {
            "membership_id": membership.id,
            "club_id": club.id,
            "club_name": club.name,
        }
        for membership, club in rows
    ]


def respond_to_invitation(
    db: Session,
    teacher: Account,
    club_id: UUID,
    decision: str,
    membership_id: UUID | None = None,
):
    check_teacher(teacher)

    locked = lock_club_for_year_write(db, teacher.school_id, club_id)
    if locked is None:
        raise ClubRequestError(404, "Invitation not found.")
    club, year = locked

    query = select(ClubMembership).where(
        ClubMembership.club_id == club.id,
        ClubMembership.account_id == teacher.id,
        ClubMembership.role == ClubRole.TEACHER_REP,
    )
    if membership_id is not None:
        query = query.where(ClubMembership.id == membership_id)
    else:
        query = query.order_by(
            # Prefer the current invitation, including acceptance retries.
            ClubMembership.status.in_(
                (MembershipStatus.PENDING, MembershipStatus.ACTIVE)
            ).desc(),
            ClubMembership.created_at.desc(),
            ClubMembership.id.desc(),
        )
    membership = db.scalar(query.limit(1))

    if membership is None:
        raise ClubRequestError(404, "Invitation not found.")
    if club.rolled_over_from_club_id is not None and membership_id is None:
        raise ClubRequestError(422, "Membership ID is required to reply to this invitation.")

    if club.status in (ClubStatus.REJECTED, ClubStatus.ARCHIVED):
        raise ClubRequestError(409, "Club is not accepting invitations.")

    target = (
        MembershipStatus.ACTIVE
        if decision == "ACCEPT"
        else MembershipStatus.DECLINED
    )

    # Duplicate requests have no additional effect.
    if membership.status == target:
        return {
            "membership_id": membership.id,
            "club_id": club.id,
            "status": membership.status,
        }

    if membership.status != MembershipStatus.PENDING:
        raise ClubRequestError(409, "Invitation already resolved.")

    if year.status != AcademicYearStatus.ACTIVE:
        raise ClubRequestError(409, "Academic year is not active.")

    try:
        if club.rolled_over_from_club_id is not None:
            active_teacher = db.scalar(select(Account.id).where(
                Account.id == teacher.id,
                Account.school_id == club.school_id,
                Account.account_type == AccountType.TEACHER,
                Account.is_active.is_(True),
            ).with_for_update(read=True))
            if active_teacher is None:
                raise ClubRequestError(403, "Permission denied.")
        require_completion_window_open(db, year, ClubRequestError)
        membership.status = target

        if target == MembershipStatus.DECLINED:
            membership.end_date = date.today()

        db.add(AuditLog(
            actor_account_id=teacher.id,
            action=(
                "TEACHER_INVITATION_ACCEPTED"
                if decision == "ACCEPT"
                else "TEACHER_INVITATION_DECLINED"
            ),
            entity_type="club_membership",
            entity_id=str(membership.id),
            details={"club_id": str(club.id)},
        ))

        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Teacher invitation response failed (%s)", type(exc).__name__)
        raise ClubRequestError(503, "Teacher invitation response is temporarily unavailable.") from exc
    except Exception:
        db.rollback()
        raise

    return {
        "membership_id": membership.id,
        "club_id": club.id,
        "status": membership.status,
    }
