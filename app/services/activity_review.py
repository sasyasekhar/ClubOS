
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.school import School
from app.models.club_membership import ClubMembership
from app.models.activity import Activity
from app.models.activity_revision import ActivityRevision
from app.models.activity_review import ActivityReview
from app.models.audit_log import AuditLog
from app.models.enums import (
    AccountType,
    ClubRole,
    ClubStatus,
    MembershipStatus,
    ActivityRevisionStatus,
    ActivityReviewDecision,
    ReviewMode,
)
from app.schemas.activity import ActivityReviewRequest
from app.services.academic_year_write_guard import lock_club_for_year_write, require_completion_window_open
from app.services.activity_drafts import ActivityError, require_active_activity_year
from app.services.impersonation import ImpersonationError, impersonation_operation, require_active_impersonation
from app.services.sessions import AuthenticationContext, check_impersonation_deadline


STATUS_MAP = {
    ActivityReviewDecision.APPROVED: ActivityRevisionStatus.APPROVED,
    ActivityReviewDecision.REJECTED: ActivityRevisionStatus.REJECTED,
    ActivityReviewDecision.CHANGES_REQUESTED:
        ActivityRevisionStatus.CHANGES_REQUESTED,
}


def review_activity(
    db: Session,
    teacher: Account,
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    data: ActivityReviewRequest,
    idempotency_key: UUID,
):
    try:
        return _review_activity(db, teacher, club_id, activity_id, revision_id, data, idempotency_key)
    except Exception:
        db.rollback()
        raise


def review_impersonated_activity(
    db: Session,
    context: AuthenticationContext,
    expected_impersonation_session_id: UUID,
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    data: ActivityReviewRequest,
    idempotency_key: UUID,
):
    with impersonation_operation(db):
        require_active_impersonation(context)
        if context.impersonation_session_id != expected_impersonation_session_id:
            raise ImpersonationError(409, "The impersonation session has changed.")
        try:
            return _review_activity(
                db, context.effective_account, club_id, activity_id, revision_id, data, idempotency_key,
                impersonation=context,
            )
        except ActivityError:
            # A business guard may deny the operation after a lock wait, before
            # the usual admission check. Persist an expiration discovered here too.
            check_impersonation_deadline(db, context)
            raise


def _review_activity(
    db: Session,
    teacher: Account,
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    data: ActivityReviewRequest,
    idempotency_key: UUID,
    *,
    impersonation: AuthenticationContext | None = None,
):
    if (
        teacher.account_type != AccountType.TEACHER
        or not teacher.is_active
        or teacher.school_id is None
    ):
        raise ActivityError(403, "Permission denied.")

    locked = lock_club_for_year_write(db, teacher.school_id, club_id)
    if locked is None:
        raise ActivityError(404, "Club not found.")
    club, year = locked

    if club.status != ClubStatus.ACTIVE:
        raise ActivityError(409, "Club is not active.")

    school = db.get(School, club.school_id)
    if school is None or not school.is_active:
        raise ActivityError(409, "Academic year is not active.")

    # Membership writes take this same club lock before updating the assignment.
    assigned = db.scalar(select(ClubMembership.id).where(
        ClubMembership.club_id == club.id,
        ClubMembership.account_id == teacher.id,
        ClubMembership.role == ClubRole.TEACHER_REP,
        ClubMembership.status == MembershipStatus.ACTIVE,
    ))
    if assigned is None:
        raise ActivityError(403, "Teacher is not assigned.")

    activity = db.scalar(select(Activity).where(Activity.id == activity_id, Activity.club_id == club.id))
    if activity is None:
        raise ActivityError(404, "Activity not found.")

    revision = db.scalar(
        select(ActivityRevision)
        .where(ActivityRevision.id == revision_id, ActivityRevision.activity_id == activity.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if revision is None:
        raise ActivityError(404, "Revision not found.")

    require_active_activity_year(db, year)
    if impersonation is None:
        actor_id, represented_id, session_id, mode = teacher.id, None, None, ReviewMode.DIRECT
        review_id = uuid5(
            NAMESPACE_URL,
            f"clubos:activity-review:{teacher.id}:{revision_id}:{idempotency_key}",
        )
    else:
        actor_id = impersonation.actual_account.id
        represented_id = teacher.id
        session_id = impersonation.impersonation_session_id
        mode = ReviewMode.IMPERSONATION
        review_id = uuid5(NAMESPACE_URL, f"clubos:activity-review:impersonation:{session_id}:{idempotency_key}")

    existing = db.get(ActivityReview, review_id)
    if existing is not None:
        if impersonation is not None:
            checked_at = require_completion_window_open(db, year, ActivityError)
            check_impersonation_deadline(db, impersonation, checked_at)
        if (
            existing.actor_account_id != actor_id
            or existing.represented_account_id != represented_id
            or existing.impersonation_session_id != session_id
            or existing.review_mode != mode
            or existing.revision_id != revision.id
            or existing.decision != data.decision
            or existing.comments != data.comments
        ):
            raise ActivityError(409, "Idempotency key conflict.")
        return {
            "review_id": existing.id,
            "revision_id": revision.id,
            "decision": existing.decision,
            "status": STATUS_MAP[existing.decision],
        }

    previous = None
    if revision.status == ActivityRevisionStatus.PENDING and data.decision == ActivityReviewDecision.APPROVED:
        previous = db.scalar(
            select(ActivityRevision)
            .where(ActivityRevision.activity_id == activity.id, ActivityRevision.is_published.is_(True))
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    # Both deadlines use the same PostgreSQL sample after all business locks.
    reviewed_at = require_completion_window_open(db, year, ActivityError)
    if impersonation is not None:
        check_impersonation_deadline(db, impersonation, reviewed_at)
    if revision.status != ActivityRevisionStatus.PENDING:
        raise ActivityError(409, "Revision is not awaiting review.")

    if previous is not None:
        previous.is_published = False
        previous.superseded_at = reviewed_at
        db.flush()

    revision.status = STATUS_MAP[data.decision]
    revision.is_published = data.decision == ActivityReviewDecision.APPROVED

    review = ActivityReview(
        id=review_id,
        revision_id=revision.id,
        actor_account_id=actor_id,
        represented_account_id=represented_id,
        impersonation_session_id=session_id,
        review_mode=mode,
        decision=data.decision,
        comments=data.comments,
        reviewed_at=reviewed_at,
    )
    audit = AuditLog(
        actor_account_id=actor_id,
        represented_account_id=represented_id,
        impersonation_session_id=session_id,
        action="ACTIVITY_REVIEWED",
        entity_type="activity_revision",
        entity_id=str(revision.id),
        details={"review_id": str(review_id), "decision": data.decision.value},
        occurred_at=reviewed_at,
    )
    db.add_all([review, audit])
    result = {"review_id": review_id, "revision_id": revision.id, "decision": review.decision, "status": revision.status}
    db.commit()
    return result



