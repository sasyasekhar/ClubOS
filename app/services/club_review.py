
import logging
from datetime import date
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.school import School
from app.models.school_admin import SchoolAdmin
from app.models.club_review import ClubReview
from app.models.club_membership import ClubMembership
from app.models.audit_log import AuditLog
from app.models.enums import (
    AccountType,
    AcademicYearStatus,
    ClubStatus,
    ClubReviewDecision,
    ClubRole,
    MembershipStatus,
)
from app.schemas.club import ClubReviewRequest
from app.services.academic_year_write_guard import lock_club_for_year_write, require_completion_window_open
from app.services.club_registration import ClubRequestError
from app.services.representative_applications import selected_representative_account_id


logger = logging.getLogger(__name__)

STATUS_MAP = {
    ClubReviewDecision.APPROVED: ClubStatus.ACTIVE,
    ClubReviewDecision.REJECTED: ClubStatus.REJECTED,
    ClubReviewDecision.CHANGES_REQUESTED:
        ClubStatus.CHANGES_REQUESTED,
}


def review_club(
    db: Session,
    admin: Account,
    club_id: UUID,
    data: ClubReviewRequest,
    idempotency_key: UUID,
):
    if (
        admin.account_type != AccountType.SCHOOL_ADMIN
        or not admin.is_active
        or admin.school_id is None
    ):
        raise ClubRequestError(403, "Permission denied.")

    # Take the review's school-admin foreign-key lock before the year lock.
    admin_link = db.scalar(
        select(SchoolAdmin.account_id)
        .where(SchoolAdmin.school_id == admin.school_id, SchoolAdmin.account_id == admin.id)
        .with_for_update(read=True, key_share=True, of=SchoolAdmin)
    )
    school = db.get(School, admin.school_id)

    if (
        admin_link is None
        or school is None
        or not school.is_active
    ):
        raise ClubRequestError(403, "Permission denied.")

    locked = lock_club_for_year_write(db, admin.school_id, club_id)
    if locked is None:
        raise ClubRequestError(404, "Club not found.")
    club, year = locked

    review_id = uuid5(
        NAMESPACE_URL,
        f"clubos:club-review:{admin.id}:{club_id}:{idempotency_key}",
    )

    existing = db.get(ClubReview, review_id)

    if existing:
        if (
            existing.decision != data.decision
            or existing.comments != data.comments
        ):
            raise ClubRequestError(
                409, "Idempotency key reused with different data."
            )

        return {
            "id": existing.id,
            "club_id": club.id,
            "decision": existing.decision,
            "club_status": STATUS_MAP[existing.decision],
        }

    if club.status != ClubStatus.PENDING:
        raise ClubRequestError(
            409, "Club is not awaiting review."
        )

    student_id = club.created_by
    if club.rolled_over_from_club_id is not None:
        student_id = selected_representative_account_id(db, club)
        if student_id is None:
            raise ClubRequestError(409, "This carried-forward club is awaiting fresh representative assignments.")
        if data.decision != ClubReviewDecision.APPROVED:
            raise ClubRequestError(409, "Only activation can be reviewed for a carried-forward club.")

    if data.decision == ClubReviewDecision.APPROVED:
        student = db.scalar(select(Account).where(
            Account.id == student_id,
            Account.school_id == club.school_id,
            Account.account_type == AccountType.STUDENT,
            Account.is_active.is_(True),
        ).with_for_update(read=True))

        if year.status != AcademicYearStatus.ACTIVE or student is None:
            raise ClubRequestError(
                409, "Club cannot be approved in its current state."
            )
        if club.rolled_over_from_club_id is not None:
            accepted_teacher = db.scalar(
                select(Account.id)
                .join(ClubMembership, ClubMembership.account_id == Account.id)
                .where(
                    ClubMembership.club_id == club.id,
                    ClubMembership.role == ClubRole.TEACHER_REP,
                    ClubMembership.status == MembershipStatus.ACTIVE,
                    Account.school_id == club.school_id,
                    Account.account_type == AccountType.TEACHER,
                    Account.is_active.is_(True),
                )
                .with_for_update(read=True, of=Account)
            )
            if accepted_teacher is None:
                raise ClubRequestError(409, "The teacher representative must accept the invitation before activation.")
    elif year.status != AcademicYearStatus.ACTIVE:
        raise ClubRequestError(409, "Academic year is not active.")

    review = ClubReview(
        id=review_id,
        club_id=club.id,
        admin_account_id=admin.id,
        decision=data.decision,
        comments=data.comments,
    )

    audit = AuditLog(
        actor_account_id=admin.id,
        action="CLUB_REVIEWED",
        entity_type="club",
        entity_id=str(club.id),
        details={
            "decision": data.decision.value,
            "review_id": str(review_id),
        },
    )

    try:
        require_completion_window_open(db, year, ClubRequestError)
        if data.decision == ClubReviewDecision.APPROVED:
            db.add(ClubMembership(
                club_id=club.id,
                account_id=student.id,
                role=ClubRole.STUDENT_REP,
                status=MembershipStatus.ACTIVE,
                start_date=date.today(),
            ))
        club.status = STATUS_MAP[data.decision]
        db.add_all([review, audit])
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Club review failed (%s)", type(exc).__name__)
        raise ClubRequestError(503, "Club review is temporarily unavailable.") from exc
    except Exception:
        db.rollback()
        raise

    return {
        "id": review.id,
        "club_id": club.id,
        "decision": review.decision,
        "club_status": club.status,
    }
