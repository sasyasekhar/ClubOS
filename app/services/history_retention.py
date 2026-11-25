from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.auth_session import AuthSession
from app.models.impersonation_session import ImpersonationSession
from app.models.login_history import LoginHistory
from app.services.sessions import database_now


RETENTION_PERIOD = timedelta(days=30)


def cleanup_history(
    db: Session, *, limit: int, now: datetime | None = None, dry_run: bool = False,
) -> dict[str, int]:
    if not 1 <= limit <= 500:
        raise ValueError("Batch size must be between 1 and 500.")
    with db.begin():
        checked_at = now if now is not None else database_now(db)
        if checked_at.utcoffset() is None:
            raise ValueError("Timezone-aware datetime required.")
        cutoff = checked_at.astimezone(timezone.utc) - RETENTION_PERIOD
        audit, login, sessions = AuditLog.__table__, LoginHistory.__table__, AuthSession.__table__
        bound = select(ImpersonationSession.id).where(ImpersonationSession.auth_session_id == sessions.c.id).exists()
        results = {}
        for table, timestamp, eligible in (
            (audit, audit.c.occurred_at, audit.c.occurred_at <= cutoff),
            (login, login.c.occurred_at, login.c.occurred_at <= cutoff),
            (sessions, sessions.c.expires_at, (sessions.c.expires_at <= cutoff) & ~bound),
        ):
            batch = select(table.c.id).where(eligible).order_by(timestamp).limit(limit)
            if dry_run:
                results[table.name] = db.scalar(select(func.count()).select_from(batch.subquery()))
            else:
                locked = batch.with_for_update(skip_locked=True).cte("expired")
                result = db.execute(delete(table).where(table.c.id.in_(select(locked.c.id))))
                results[table.name] = result.rowcount
        return results
