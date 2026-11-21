
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.activity_revision import ActivityRevision
from app.models.activity_attachment import ActivityAttachment
from app.models.audit_log import AuditLog

RETENTION = timedelta(days=30)


def purge_expired_superseded_content(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    now = now or datetime.now(timezone.utc)

    if now.utcoffset() is None:
        raise ValueError("Timezone-aware datetime required")

    if not 1 <= limit <= 500:
        raise ValueError("Invalid batch size")

    try:
        revisions = db.scalars(
            select(ActivityRevision)
            .where(
                ActivityRevision.superseded_at <= now - RETENTION,
                ActivityRevision.content_purged_at.is_(None),
                ActivityRevision.is_published.is_(False),
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

        # Never purge database content while attachment objects
        # could still exist in private storage.
        attached_ids = set(db.scalars(
            select(ActivityAttachment.revision_id)
            .where(
                ActivityAttachment.revision_id.in_(revision_ids)
            )
        ).all())

        purged = 0

        for revision in revisions:
            if revision.id in attached_ids:
                continue

            # Historical audit snapshots must not retain
            # copies of content that has been purged.
            logs = db.scalars(
                select(AuditLog).where(
                    or_(
                        and_(
                            AuditLog.action == "ACTIVITY_DRAFT_CREATED",
                            AuditLog.details["revision_id"].astext
                            == str(revision.id),
                        ),
                        and_(
                            AuditLog.action.in_([
                                "ACTIVITY_REPLACEMENT_STARTED",
                                "ACTIVITY_CORRECTION_STARTED",
                            ]),
                            AuditLog.entity_id == str(revision.id),
                        ),
                    )
                )
            ).all()

            for log in logs:
                details = dict(log.details or {})
                if "payload" in details:
                    details.pop("payload")
                    details["content_purged"] = True
                    log.details = details

            revision.title = None
            revision.activity_date = None
            revision.description = None
            revision.category = None
            revision.content_purged_at = now

            purged += 1

        db.commit()
        return purged

    except Exception:
        db.rollback()
        raise
