
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.audit_log import AuditLog
from app.models.enums import (
    AccountType,
    ClubRole,
    ClubStatus,
    MembershipStatus,
)
from app.services.club_registration import ClubRequestError


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
):
    check_teacher(teacher)

    # Serialize decisions for this club, including admin reviews.
    club = db.scalar(
        select(Club)
        .where(
            Club.id == club_id,
            Club.school_id == teacher.school_id,
        )
        .with_for_update()
    )

    if club is None:
        raise ClubRequestError(404, "Invitation not found.")

    membership = db.scalar(
        select(ClubMembership)
        .where(
            ClubMembership.club_id == club.id,
            ClubMembership.account_id == teacher.id,
            ClubMembership.role == ClubRole.TEACHER_REP,
        )
        .order_by(
            ClubMembership.created_at.desc(),
            ClubMembership.id.desc(),
        )
        .limit(1)
    )

    if membership is None:
        raise ClubRequestError(404, "Invitation not found.")

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

    try:
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
    except Exception:
        db.rollback()
        raise

    return {
        "membership_id": membership.id,
        "club_id": club.id,
        "status": membership.status,
    }
