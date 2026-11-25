
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
from app.models.enums import ActivityRevisionStatus
from app.schemas.activity import ActivityDraftCreate
from app.services.academic_year_write_guard import require_completion_window_open
from app.services.activity_drafts import ActivityError
from app.services.activity_workflow import (
    authorized_revision,
    response_for,
)
from app.services.operation_receipts import find_receipt, stage_receipt


def create_replacement_draft(
    db: Session,
    student: Account,
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    data: ActivityDraftCreate,
    idempotency_key: UUID,
):
    try:
        source, year = authorized_revision(
            db, student, club_id, activity_id, revision_id
        )

        unique = (
            f"{student.id}:{activity_id}:"
            f"{revision_id}:{idempotency_key}"
        )
        new_id = uuid5(
            NAMESPACE_URL, f"clubos:replacement:{unique}"
        )
        audit_id = uuid5(
            NAMESPACE_URL, f"clubos:replacement-audit:{unique}"
        )
        payload = data.model_dump(mode="json")

        action = "ACTIVITY_REPLACEMENT_STARTED"
        if find_receipt(db, audit_id, action, payload, ActivityError) is not None:
            revision = db.get(ActivityRevision, new_id)
            if revision is None:
                raise ActivityError(409, "Replacement unavailable.")

            return response_for(revision)

        require_completion_window_open(db, year, ActivityError)
        if (
            source.status != ActivityRevisionStatus.APPROVED
            or not source.is_published
        ):
            raise ActivityError(409, "Source is not published.")

        latest = db.scalar(
            select(ActivityRevision)
            .where(ActivityRevision.activity_id == activity_id)
            .order_by(ActivityRevision.revision_number.desc())
            .limit(1)
        )

        if latest is None:
            raise ActivityError(404, "Activity revision not found.")

        if latest.status in (
            ActivityRevisionStatus.DRAFT,
            ActivityRevisionStatus.PENDING,
            ActivityRevisionStatus.CHANGES_REQUESTED,
        ):
            raise ActivityError(
                409, "Another revision needs to be completed first."
            )

        revision = ActivityRevision(
            id=new_id,
            activity_id=activity_id,
            revision_number=latest.revision_number + 1,
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
            action=action,
            entity_type="activity_revision",
            entity_id=str(new_id),
            details={
                "source_revision_id": str(source.id),
            },
        )

        stage_receipt(db, audit_id, action, payload)
        db.add_all([revision, audit])
        db.commit()
        return response_for(revision)

    except IntegrityError as exc:
        db.rollback()
        raise ActivityError(
            409, "Replacement could not be created."
        ) from exc

    except Exception:
        db.rollback()
        raise
