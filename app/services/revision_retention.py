
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.models.activity_revision import ActivityRevision
from app.models.activity_attachment import ActivityAttachment
from app.models.audit_log import AuditLog
from app.services.activity_attachments import has_managed_object
from app.services.history_retention import RETENTION_PERIOD
from app.services.sessions import database_now
from app.services.storage import AttachmentStorage, StorageError, get_attachment_storage

logger = logging.getLogger(__name__)


@dataclass
class RevisionCleanup:
    purged: int = 0
    failed: int = 0


def _due_revisions(now: datetime):
    legacy_attachments = select(ActivityAttachment.id).where(
        ActivityAttachment.revision_id == ActivityRevision.id,
        ActivityAttachment.size_bytes.is_(None),
    ).exists()
    return select(ActivityRevision).where(
        ActivityRevision.superseded_at <= now - RETENTION_PERIOD,
        ActivityRevision.content_purged_at.is_(None),
        ActivityRevision.is_published.is_(False),
        ~legacy_attachments,
    )


def preview_revision_cleanup(db: Session, now: datetime, limit: int) -> int:
    batch = _due_revisions(now).with_only_columns(ActivityRevision.id).limit(limit)
    return db.scalar(select(func.count()).select_from(batch.subquery()))


def purge_expired_superseded_content(
    db: Session,
    *,
    storage: AttachmentStorage | None = None,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    return purge_superseded_batch(db, storage=storage, now=now, limit=limit).purged


def purge_superseded_batch(
    db: Session, *, storage: AttachmentStorage | None, now: datetime | None = None, limit: int,
) -> RevisionCleanup:
    now = now if now is not None else database_now(db)

    if now.utcoffset() is None:
        raise ValueError("Timezone-aware datetime required")

    if not 1 <= limit <= 500:
        raise ValueError("Invalid batch size")

    now = now.astimezone(timezone.utc)
    result = RevisionCleanup()
    try:
        revisions = db.scalars(
            _due_revisions(now)
            .order_by(
                func.coalesce(ActivityRevision.purge_attempted_at, ActivityRevision.superseded_at),
                ActivityRevision.id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()

        if not revisions:
            db.commit()
            return result

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
                revision.purge_attempted_at = max(now, database_now(db))
                result.failed += 1
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
                    revision.purge_attempted_at = max(now, database_now(db))
                    result.failed += 1
                    continue
                for attachment in attachments:
                    db.delete(attachment)

            revision.title = None
            revision.activity_date = None
            revision.description = None
            revision.category = None
            revision.content_purged_at = now
            revision.purge_attempted_at = None

            purged_ids.append(str(revision.id))

        if purged_ids:
            _scrub_audit_content(db, purged_ids)

        db.commit()
        result.purged = len(purged_ids)
        return result

    except Exception:
        db.rollback()
        raise


def _scrub_audit_content(db: Session, revision_ids: list[str]) -> None:
    db.execute(
        update(AuditLog).where(
            AuditLog.details.has_key("payload"),
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
        ).values(details=AuditLog.details.delete_path(["payload"]).concat(func.jsonb_build_object("content_purged", True)))
    )
