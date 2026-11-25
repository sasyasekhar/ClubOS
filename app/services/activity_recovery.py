import logging
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.activity_attachment import ActivityAttachment
from app.models.activity_review import ActivityReview
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
from app.models.enums import ActivityReviewDecision, ActivityRevisionStatus
from app.schemas.activity import ActivityAttachmentResponse, PreviousActivityVersion
from app.services.academic_year_write_guard import require_completion_window_open
from app.services.activity_attachments import (
    attachment_can_be_copied, attachment_object_key, lock_new_revision_objects, read_verified_object,
)
from app.services.activity_drafts import ActivityError, require_active_activity_year
from app.services.activity_replacements import next_replacement_number
from app.services.activity_workflow import authorized_student_activity, previous_publication, recovery_deadline, response_for
from app.services.operation_receipts import find_receipt, stage_receipt
from app.services.sessions import database_now
from app.services.storage import AttachmentStorage, StorageError


logger = logging.getLogger(__name__)
RESTORED = "ACTIVITY_PREVIOUS_VERSION_RESTORED"
CONTENT_FIELDS = ("title", "description", "category", "activity_date")


def _publication_pair(db: Session, activity_id: UUID) -> tuple[UUID, ActivityRevision]:
    current = db.scalar(select(ActivityRevision.id).where(
        ActivityRevision.activity_id == activity_id, ActivityRevision.is_published.is_(True),
    ))
    previous = db.scalar(previous_publication(activity_id).with_for_update(read=True).execution_options(populate_existing=True))
    if current is None or previous is None:
        raise ActivityError(404, {"code": "ACTIVITY_NO_PREVIOUS_VERSION", "message": "No previous published version exists."})
    return current, previous


def _retained_content(source: ActivityRevision, now: datetime) -> dict:
    if now >= recovery_deadline(source.superseded_at):
        raise ActivityError(410, {"code": "ACTIVITY_RECOVERY_EXPIRED", "message": "The recovery period has ended."})
    content = {field: getattr(source, field) for field in CONTENT_FIELDS}
    if source.content_purged_at is not None or any(value is None for value in content.values()):
        raise ActivityError(410, {"code": "ACTIVITY_PREVIOUS_VERSION_UNAVAILABLE", "message": "Previous content is unavailable."})
    return content


def _attachments(db: Session, revision_id: UUID) -> list[ActivityAttachment]:
    return list(db.scalars(select(ActivityAttachment).where(
        ActivityAttachment.revision_id == revision_id,
    ).order_by(ActivityAttachment.uploaded_at, ActivityAttachment.id)))


def preview_previous_version(db: Session, student: Account, club_id: UUID, activity_id: UUID) -> PreviousActivityVersion:
    db.execute(text("SET LOCAL statement_timeout = '10s'"))
    authorized_student_activity(db, student, club_id, activity_id)
    current_id, source = _publication_pair(db, activity_id)
    attachments = _attachments(db, source.id)
    usable = [ActivityAttachmentResponse.model_validate(item) for item in attachments if attachment_can_be_copied(item)]
    published_at = db.scalar(select(ActivityReview.reviewed_at).where(
        ActivityReview.revision_id == source.id, ActivityReview.decision == ActivityReviewDecision.APPROVED,
    ).order_by(ActivityReview.reviewed_at.desc()).limit(1))
    content = _retained_content(source, database_now(db))
    return PreviousActivityVersion(
        activity_id=activity_id, current_revision_id=current_id, revision_id=source.id,
        revision_number=source.revision_number, published_at=published_at, superseded_at=source.superseded_at,
        recovery_expires_at=recovery_deadline(source.superseded_at), attachments=usable,
        unavailable_attachment_count=len(attachments) - len(usable), **content,
    )


def restore_previous_version(
    db: Session, storage: AttachmentStorage, student: Account, club_id: UUID, activity_id: UUID,
    expected_revision_id: UUID, idempotency_key: UUID,
):
    attempted_keys = []
    commit_started = False
    try:
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
        _, year = authorized_student_activity(db, student, club_id, activity_id)
        require_active_activity_year(db, year)
        operation_id = uuid5(NAMESPACE_URL, f"clubos:restore-previous:{student.id}:{activity_id}:{idempotency_key}")
        payload = str(expected_revision_id)
        receipt = find_receipt(db, operation_id, RESTORED, payload, ActivityError)
        if receipt is not None:
            existing = db.get(ActivityRevision, receipt.result_id) if receipt.result_id is not None else None
            if existing is None or existing.activity_id != activity_id:
                raise ActivityError(409, "Restoration result is unavailable.")
            return response_for(existing)

        _, source = _publication_pair(db, activity_id)
        if source.id != expected_revision_id:
            raise ActivityError(409, {"code": "ACTIVITY_PREVIOUS_VERSION_CHANGED", "message": "The previous version has changed."})
        content = _retained_content(source, require_completion_window_open(db, year, ActivityError))
        number = next_replacement_number(db, activity_id, unfinished_detail={
            "code": "ACTIVITY_UNFINISHED_REVISION", "message": "Another revision needs to be completed first.",
        })
        draft = ActivityRevision(
            id=uuid4(), activity_id=activity_id, revision_number=number,
            status=ActivityRevisionStatus.DRAFT, is_published=False, **content,
        )
        originals = _attachments(db, source.id)
        if any(not attachment_can_be_copied(item) for item in originals):
            raise ActivityError(409, {"code": "ACTIVITY_PREVIOUS_ATTACHMENT_UNVERIFIED",
                                      "message": "A previous attachment lacks verified scan or storage metadata."})
        db.add(draft)
        db.flush()
        if originals:
            lock_new_revision_objects(db, draft.id)
        for old in originals:
            data = read_verified_object(storage, old.storage_key, old.size_bytes, old.content_sha256)
            attachment_id = uuid4()
            key = attachment_object_key(draft.id, attachment_id)
            attempted_keys.append(key)
            storage.put(key, data, old.file_type)
            read_verified_object(storage, key, old.size_bytes, old.content_sha256)
            db.add(ActivityAttachment(
                id=attachment_id, revision_id=draft.id, storage_key=key,
                file_name=old.file_name, file_type=old.file_type, idempotency_key=uuid4(),
                size_bytes=old.size_bytes, content_sha256=old.content_sha256, scan_passed_at=old.scan_passed_at,
            ))
        stage_receipt(db, operation_id, RESTORED, payload, result_id=draft.id)
        db.add(AuditLog(
            id=operation_id, actor_account_id=student.id, action=RESTORED, entity_type="activity_revision",
            entity_id=str(draft.id), details={"source_revision_id": str(source.id)}, occurred_at=database_now(db),
        ))
        db.flush()
        # Sample PostgreSQL time after the lock waits, file copies and metadata writes.
        _retained_content(source, require_completion_window_open(db, year, ActivityError))
        result = response_for(draft)
        # A failed commit acknowledgement may still mean the rows committed.
        commit_started = True
        db.commit()
        return result
    except (StorageError, OSError) as exc:
        raise ActivityError(503, {"code": "ACTIVITY_PREVIOUS_ATTACHMENT_UNAVAILABLE",
                                  "message": "A previous attachment could not be copied and verified."}) from exc
    finally:
        if db.in_transaction():
            db.rollback()
        if not commit_started:
            for key in attempted_keys:
                try:
                    storage.delete(key)
                except (StorageError, OSError):
                    logger.warning("A previous-version restoration object needs orphan reconciliation.")
