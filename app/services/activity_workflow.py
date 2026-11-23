
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.school import School
from app.models.club_membership import ClubMembership
from app.models.activity import Activity
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
from app.models.enums import (
    AccountType,
    AcademicYearStatus,
    ClubStatus,
    ClubRole,
    MembershipStatus,
    ActivityRevisionStatus,
)
from app.schemas.activity import ActivityDraftCreate
from app.services.academic_year_write_guard import lock_club_for_year_write
from app.services.activity_drafts import ActivityError


def response_for(revision):
    return {
        "activity_id": revision.activity_id,
        "revision_id": revision.id,
        "revision_number": revision.revision_number,
        "status": revision.status,
    }


def authorized_revision(
    db: Session,
    student: Account,
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
):
    if (
        student.account_type != AccountType.STUDENT
        or not student.is_active
        or student.school_id is None
    ):
        raise ActivityError(403, "Permission denied.")

    locked = lock_club_for_year_write(db, student.school_id, club_id)
    if locked is None:
        raise ActivityError(404, "Club not found.")
    club, year = locked

    if club.status != ClubStatus.ACTIVE:
        raise ActivityError(409, "Club is not active.")

    school = db.get(School, club.school_id)

    if (
        school is None
        or not school.is_active
        or year.status != AcademicYearStatus.ACTIVE
    ):
        raise ActivityError(409, "Academic year is not active.")

    is_rep = db.scalar(
        select(ClubMembership.id).where(
            ClubMembership.club_id == club.id,
            ClubMembership.account_id == student.id,
            ClubMembership.role == ClubRole.STUDENT_REP,
            ClubMembership.status == MembershipStatus.ACTIVE,
        )
    )

    if is_rep is None:
        raise ActivityError(403, "Permission denied.")

    activity = db.scalar(
        select(Activity).where(
            Activity.id == activity_id,
            Activity.club_id == club.id,
        )
    )

    if activity is None:
        raise ActivityError(404, "Activity not found.")

    revision = db.scalar(
        select(ActivityRevision)
        .where(
            ActivityRevision.id == revision_id,
            ActivityRevision.activity_id == activity.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    if revision is None:
        raise ActivityError(404, "Revision not found.")

    return revision


def edit_activity_draft(
    db: Session,
    student: Account,
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    data: ActivityDraftCreate,
):
    try:
        revision = authorized_revision(
            db, student, club_id, activity_id, revision_id
        )

        if revision.status != ActivityRevisionStatus.DRAFT:
            raise ActivityError(409, "Revision is not editable.")

        values = data.model_dump()
        changed_fields = [
            field for field, value in values.items()
            if getattr(revision, field) != value
        ]

        # Identical PUT requests do not produce additional writes.
        if not changed_fields:
            return response_for(revision)

        for field, value in values.items():
            setattr(revision, field, value)

        db.add(AuditLog(
            actor_account_id=student.id,
            action="ACTIVITY_DRAFT_UPDATED",
            entity_type="activity_revision",
            entity_id=str(revision.id),
            details={"changed_fields": changed_fields},
        ))

        db.commit()
        return response_for(revision)

    except Exception:
        db.rollback()
        raise


def submit_activity_draft(
    db: Session,
    student: Account,
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
):
    try:
        revision = authorized_revision(
            db, student, club_id, activity_id, revision_id
        )

        # A repeated submission is a successful no-op.
        if revision.status == ActivityRevisionStatus.PENDING:
            return response_for(revision)

        if revision.status != ActivityRevisionStatus.DRAFT:
            raise ActivityError(409, "Revision cannot be submitted.")

        revision.status = ActivityRevisionStatus.PENDING
        revision.submitted_at = datetime.now(timezone.utc)

        db.add(AuditLog(
            actor_account_id=student.id,
            action="ACTIVITY_SUBMITTED",
            entity_type="activity_revision",
            entity_id=str(revision.id),
            details={"activity_id": str(activity_id)},
        ))

        db.commit()
        return response_for(revision)

    except Exception:
        db.rollback()
        raise
