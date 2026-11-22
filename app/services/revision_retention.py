
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.activity_revision import ActivityRevision
from app.models.activity_attachment import ActivityAttachment
from app.models.audit_log import AuditLog
from app.services.activity_attachments import has_managed_object
from app.services.storage import AttachmentStorage, StorageError, get_attachment_storage

logger = logging.getLogger(__name__)
RETENTION = timedelta(days=30)


def purge_expired_superseded_content(
    db: Session,
    *,
    storage: AttachmentStorage | None = None,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    now = now or datetime.now(timezone.utc)

    if now.utcoffset() is None:
        raise ValueError("Timezone-aware datetime required")

    if not 1 <= limit <= 500:
        raise ValueError("Invalid batch size")

    try:
        legacy_attachments = (
            select(ActivityAttachment.id)
            .where(
                ActivityAttachment.revision_id == ActivityRevision.id,
                ActivityAttachment.size_bytes.is_(None),
            )
            .exists()
        )
        revisions = db.scalars(
            select(ActivityRevision)
            .where(
                ActivityRevision.superseded_at <= now - RETENTION,
                ActivityRevision.content_purged_at.is_(None),
                ActivityRevision.is_published.is_(False),
                ~legacy_attachments,
            )
            .order_by(
                ActivityRevision.superseded_at,
                ActivityRevision.id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()

        if not revisions:
            db.commit()
            return 0

        revision_ids = [revision.id for revision in revisions]

        attachments_by_revision: dict[UUID, list[ActivityAttachment]] = defaultdict(list)
        attachments = db.scalars(
            select(ActivityAttachment)
            .where(ActivityAttachment.revision_id.in_(revision_ids))
            .order_by(ActivityAttachment.id)
        )
        for attachment in attachments:
            attachments_by_revision[attachment.revision_id].append(attachment)

        purged_ids = []

        for revision in revisions:
            attachments = attachments_by_revision[revision.id]
            if any(not has_managed_object(attachment) for attachment in attachments):
                logger.error("Unverified attachment storage keys for revision %s", revision.id)
                continue

            if attachments:
                try:
                    if storage is None:
                        storage = get_attachment_storage()
                    for attachment in attachments:
                        storage.delete(attachment.storage_key)
                except StorageError:
                    logger.error(
                        "Attachment deletion failed for revision %s. Cleanup can be retried.",
                        revision.id,
                    )
                    continue
                for attachment in attachments:
                    db.delete(attachment)

            revision.title = None
            revision.activity_date = None
            revision.description = None
            revision.category = None
            revision.content_purged_at = now

            purged_ids.append(str(revision.id))

        if purged_ids:
            _scrub_audit_content(db, purged_ids)

        db.commit()
        return len(purged_ids)

    except Exception:
        db.rollback()
        raise


def _scrub_audit_content(db: Session, revision_ids: list[str]) -> None:
    logs = db.scalars(
        select(AuditLog).where(
            or_(
                and_(
                    AuditLog.action == "ACTIVITY_DRAFT_CREATED",
                    AuditLog.details["revision_id"].astext.in_(revision_ids),
                ),
                and_(
                    AuditLog.action.in_([
                        "ACTIVITY_REPLACEMENT_STARTED",
                        "ACTIVITY_CORRECTION_STARTED",
                    ]),
                    AuditLog.entity_id.in_(revision_ids),
                ),
            )
        )
    )
    for log in logs:
        details = dict(log.details or {})
        if "payload" in details:
            details.pop("payload")
            details["content_purged"] = True
            log.details = details
