from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.activity import Activity
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
from app.models.deleted_record import DeletedRecord
from app.models.enums import ActivityRevisionStatus, ClubStatus, DeletionStatus
from app.services.academic_year_write_guard import lock_club_for_year_write, require_completion_window_open
from app.services.activity_drafts import ActivityError, require_active_activity_year
from app.services.history_retention import RETENTION_PERIOD
from app.services.sessions import database_now


DRAFT = "ACTIVITY_DRAFT"


def _lock_revision(db: Session, actor: Account, club_id: UUID, revision_id: UUID):
    if not actor.is_active or actor.school_id is None:
        raise ActivityError(403, "Permission denied.")
    locked = lock_club_for_year_write(db, actor.school_id, club_id)
    if locked is None:
        raise ActivityError(404, "Recovery record not found.")
    club, year = locked
    revision = db.scalar(
        select(ActivityRevision).join(Activity, Activity.id == ActivityRevision.activity_id)
        .where(Activity.club_id == club.id, ActivityRevision.id == revision_id)
        .with_for_update(of=ActivityRevision).execution_options(populate_existing=True)
    )
    if revision is None:
        raise ActivityError(404, "Recovery record not found.")
    if club.status != ClubStatus.ACTIVE:
        raise ActivityError(409, "Club is not active.")
    require_active_activity_year(db, year)
    return revision, year


def _require_retained_draft(revision: ActivityRevision) -> None:
    if (revision.status != ActivityRevisionStatus.DRAFT or revision.is_published
            or revision.superseded_at is not None or revision.content_purged_at is not None):
        raise ActivityError(409, "Draft content is unavailable for recovery.")


def _audit(db: Session, record: DeletedRecord, actor: Account, action: str, stamped_at: datetime) -> None:
    db.add(AuditLog(actor_account_id=actor.id, action=action, entity_type="deleted_record",
                    entity_id=str(record.id), occurred_at=stamped_at))


def stage_draft_recovery(
    db: Session, actor: Account, club_id: UUID, revision_id: UUID, operation_key: UUID,
) -> DeletedRecord:
    """Stage a recovery marker in an approved caller's deletion transaction."""
    revision, year = _lock_revision(db, actor, club_id, revision_id)
    record_id = uuid5(NAMESPACE_URL, f"clubos:draft-recovery:{actor.school_id}:{actor.id}:{revision.id}:{operation_key}")
    existing = db.get(DeletedRecord, record_id)
    if existing is not None:
        return existing
    _require_retained_draft(revision)
    pending = select(DeletedRecord.id).where(
        DeletedRecord.revision_id == revision.id, DeletedRecord.status == DeletionStatus.PENDING,
        DeletedRecord.school_id.is_not(None),
    ).exists()
    if db.scalar(select(pending)):
        raise ActivityError(409, "A recovery record already exists for this draft.")
    deleted_at = require_completion_window_open(db, year, ActivityError)
    record = DeletedRecord(
        id=record_id, school_id=actor.school_id, entity_type=DRAFT, entity_id=str(revision.id),
        revision_id=revision.id, deleted_by=actor.id, deleted_at=deleted_at,
        expires_at=deleted_at.astimezone(timezone.utc) + RETENTION_PERIOD, status=DeletionStatus.PENDING,
    )
    db.add(record)
    _audit(db, record, actor, "DRAFT_RECOVERY_RECORDED", deleted_at)
    db.flush()
    return record


def complete_draft_recovery(
    db: Session, actor: Account, club_id: UUID, record_id: UUID, operation_key: UUID,
) -> DeletedRecord:
    """Validate and stage the recovery lifecycle in the caller's transaction."""
    scope = (DeletedRecord.id == record_id, DeletedRecord.school_id == actor.school_id, DeletedRecord.entity_type == DRAFT)
    entity_id = db.scalar(select(DeletedRecord.entity_id).where(*scope))
    if entity_id is None or actor.school_id is None:
        raise ActivityError(404, "Recovery record not found.")
    revision, year = _lock_revision(db, actor, club_id, UUID(entity_id))
    record = db.scalar(select(DeletedRecord).where(*scope).with_for_update().execution_options(populate_existing=True))
    if record is None:
        raise ActivityError(404, "Recovery record not found.")
    if record.status == DeletionStatus.RESTORED and record.restore_key == operation_key and record.restored_by == actor.id:
        return record
    checked_at = require_completion_window_open(db, year, ActivityError)
    if (record.status != DeletionStatus.PENDING or record.revision_id != revision.id
            or checked_at.astimezone(timezone.utc) >= record.expires_at.astimezone(timezone.utc)):
        raise ActivityError(409, "Draft is no longer recoverable.")
    _require_retained_draft(revision)
    record.status = DeletionStatus.RESTORED
    record.restored_at = checked_at
    record.restored_by = actor.id
    record.restore_key = operation_key
    record.revision_id = None
    _audit(db, record, actor, "DRAFT_RECOVERY_COMPLETED", checked_at)
    db.flush()
    return record


def expire_draft_recovery(db: Session, *, limit: int, now: datetime | None = None, dry_run: bool = False) -> int:
    if not 1 <= limit <= 500:
        raise ValueError("Batch size must be between 1 and 500.")
    with db.begin():
        table = DeletedRecord.__table__
        cutoff = now if now is not None else database_now(db)
        batch = select(table.c.id).where(
            table.c.school_id.is_not(None), table.c.status == DeletionStatus.PENDING, table.c.expires_at <= cutoff,
        ).order_by(table.c.expires_at, table.c.id).limit(limit)
        if dry_run:
            return db.scalar(select(func.count()).select_from(batch.subquery()))
        locked = batch.with_for_update(skip_locked=True).cte("expired_recovery")
        result = db.execute(update(table).where(table.c.id.in_(select(locked.c.id))).values(
            status=DeletionStatus.PERMANENTLY_DELETED, revision_id=None,
        ))
        return result.rowcount
