
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
    ActivityRevisionStatus,
    ClubStatus,
    ClubRole,
    MembershipStatus,
)

from app.schemas.activity import ActivityDraftCreate
from app.services.academic_year_write_guard import lock_club_for_year_write


class ActivityError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def create_activity_draft(
    db: Session,
    student: Account,
    club_id: UUID,
    data: ActivityDraftCreate,
    idempotency_key: UUID,
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

    unique = f"{student.id}:{club_id}:{idempotency_key}"

    activity_id = uuid5(
        NAMESPACE_URL, f"clubos:activity:{unique}"
    )
    revision_id = uuid5(
        NAMESPACE_URL, f"clubos:activity-revision:{unique}"
    )
    audit_id = uuid5(
        NAMESPACE_URL, f"clubos:activity-create:{unique}"
    )

    submitted_payload = data.model_dump(mode="json")

    existing_event = db.get(AuditLog, audit_id)

    if existing_event is not None:
        details = existing_event.details or {}

        if (
            existing_event.action != "ACTIVITY_DRAFT_CREATED"
            or existing_event.actor_account_id != student.id
            or details.get("payload") != submitted_payload
        ):
            raise ActivityError(
                409, "Idempotency key reused with different data."
            )

        return {
            "activity_id": activity_id,
            "revision_id": revision_id,
            "revision_number": 1,
            "status": ActivityRevisionStatus.DRAFT,
        }

    if club.status != ClubStatus.ACTIVE:
        raise ActivityError(409, "Club is not active.")

    school = db.get(School, student.school_id)

    if (
        school is None
        or not school.is_active
        or year.status != AcademicYearStatus.ACTIVE
    ):
        raise ActivityError(
            409, "An active academic year is required."
        )

    is_rep = db.scalar(
        select(ClubMembership.id).where(
            ClubMembership.club_id == club_id,
            ClubMembership.account_id == student.id,
            ClubMembership.role == ClubRole.STUDENT_REP,
            ClubMembership.status == MembershipStatus.ACTIVE,
        )
    )

    if is_rep is None:
        raise ActivityError(
            403, "Only the active student representative can create activities."
        )

    activity = Activity(
        id=activity_id,
        club_id=club_id,
        created_by=student.id,
    )

    revision = ActivityRevision(
        id=revision_id,
        activity_id=activity_id,
        revision_number=1,
        title=data.title,
        activity_date=data.activity_date,
        description=data.description,
        category=data.category,
        status=ActivityRevisionStatus.DRAFT,
        is_published=False,
    )

    audit = AuditLog(
        id=audit_id,
        actor_account_id=student.id,
        action="ACTIVITY_DRAFT_CREATED",
        entity_type="activity",
        entity_id=str(activity_id),
        details={
            "club_id": str(club_id),
            "revision_id": str(revision_id),
            "payload": submitted_payload,
        },
    )

    try:
        db.add_all([activity, revision, audit])
        db.commit()

    except IntegrityError as exc:
        db.rollback()
        raise ActivityError(
            409, "Activity could not be created."
        ) from exc

    except Exception:
        db.rollback()
        raise

    return {
        "activity_id": activity_id,
        "revision_id": revision_id,
        "revision_number": 1,
        "status": ActivityRevisionStatus.DRAFT,
    }
