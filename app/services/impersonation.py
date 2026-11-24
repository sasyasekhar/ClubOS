import logging
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.auth_session import AuthSession
from app.models.enums import AccountType, ImpersonationStatus
from app.models.impersonation_session import ImpersonationSession
from app.schemas.activity import PublishedActivity
from app.schemas.impersonation import (
    EligibleAccount,
    EligibleAccountPage,
    EndImpersonationRequest,
    ImpersonationState,
    StartImpersonationRequest,
)
from app.services.activity_drafts import ActivityError
from app.services.activity_publications import list_published_activities
from app.services.sessions import (
    AuthenticationContext,
    eligible_impersonation_target,
    finish_impersonation,
    impersonation_invalidation_reason,
)


logger = logging.getLogger(__name__)
STARTED = "IMPERSONATION_STARTED"
PUBLICATIONS_VIEWED = "IMPERSONATION_PUBLICATIONS_VIEWED"
ACTIVE_CONFLICT = "End the active impersonation before starting another one."
TARGET_ROLES = (AccountType.STUDENT, AccountType.TEACHER)


class ImpersonationError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@contextmanager
def _operation(db: Session, *, write: bool = False) -> Iterator[None]:
    try:
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
        yield
        if write:
            db.commit()
    except ImpersonationError:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        if isinstance(exc, IntegrityError):
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if constraint == "uq_one_active_impersonation_per_admin":
                raise ImpersonationError(409, ACTIVE_CONFLICT) from exc
        logger.error("Impersonation operation failed (%s)", type(exc).__name__)
        raise ImpersonationError(503, "Impersonation is temporarily unavailable.") from exc


def require_actual_admin_school(context: AuthenticationContext) -> UUID:
    if context.authorized_school_id is None or context.actual_account.account_type != AccountType.SCHOOL_ADMIN:
        raise ImpersonationError(403, "Permission denied.")
    return context.authorized_school_id


def require_publications_viewer(context: AuthenticationContext) -> UUID:
    school_id = require_actual_admin_school(context)
    if not context.is_impersonating or context.impersonation_invalidated:
        raise ImpersonationError(403, "An active impersonation is required for this operation.")
    return school_id


def _for_tab(context: AuthenticationContext, school_id: UUID):
    return (
        ImpersonationSession.auth_session_id == context.auth_session_id,
        ImpersonationSession.admin_account_id == context.actual_account.id,
        ImpersonationSession.school_id == school_id,
    )


def _target_for_state(db: Session, session: ImpersonationSession) -> Account | None:
    return db.scalar(select(Account).where(
        Account.id == session.target_account_id,
        Account.school_id == session.school_id,
        Account.account_type.in_(TARGET_ROLES),
        Account.is_active.is_(True),
    ).execution_options(populate_existing=True))


def _state(session: ImpersonationSession, target: Account | None) -> ImpersonationState:
    return ImpersonationState(
        active=session.status == ImpersonationStatus.ACTIVE,
        impersonation_session_id=session.id,
        represented_account_id=target.id if target is not None else None,
        represented_name=target.full_name if target is not None else None,
        represented_role=target.account_type if target is not None else None,
        reason=session.reason,
        started_at=session.started_at,
        ended_at=session.ended_at,
        status=session.status,
    )


def list_eligible_accounts(
    db: Session, context: AuthenticationContext, *, limit: int, after: UUID | None, search: str | None,
) -> EligibleAccountPage:
    with _operation(db):
        school_id = require_actual_admin_school(context)
        if context.is_impersonating or context.impersonation_invalidated:
            raise ImpersonationError(403, "End impersonation before accessing the account directory.")

        query = select(Account.id.label("account_id"), Account.full_name, Account.account_type.label("role")).where(
            Account.school_id == school_id,
            Account.account_type.in_(TARGET_ROLES),
            Account.is_active.is_(True),
        )
        if after is not None:
            query = query.where(Account.id > after)
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.where(Account.full_name.ilike(f"%{escaped}%", escape="\\"))
        rows = db.execute(query.order_by(Account.id).limit(limit + 1)).mappings().all()
        items = [EligibleAccount.model_validate(row) for row in rows[:limit]]
        return EligibleAccountPage(items=items, next_cursor=items[-1].account_id if len(rows) > limit else None)


def start_impersonation(
    db: Session, context: AuthenticationContext, data: StartImpersonationRequest, idempotency_key: UUID,
) -> ImpersonationState:
    with _operation(db, write=True):
        school_id = require_actual_admin_school(context)
        admin = context.actual_account
        session_id = uuid5(NAMESPACE_URL, f"clubos:impersonation:{admin.id}:{context.auth_session_id}:{idempotency_key}")
        existing = db.scalar(select(ImpersonationSession).where(
            ImpersonationSession.id == session_id, *_for_tab(context, school_id),
        ).execution_options(populate_existing=True))
        if existing is not None:
            if existing.target_account_id != data.target_account_id or existing.reason != data.reason:
                raise ImpersonationError(409, "Idempotency key reused with different impersonation data.")
            return _state(existing, _target_for_state(db, existing))

        if context.impersonation_invalidated:
            raise ImpersonationError(403, "The previous impersonation is no longer valid. Retry from ordinary administrator mode.")
        if context.is_impersonating:
            raise ImpersonationError(409, ACTIVE_CONFLICT)
        active = db.scalar(select(ImpersonationSession).where(
            ImpersonationSession.admin_account_id == admin.id,
            ImpersonationSession.status == ImpersonationStatus.ACTIVE,
        ).with_for_update().execution_options(populate_existing=True))
        if active is not None:
            # The actual administrator row serializes lifecycle operations. A
            # second parent row lock here would invert parent-before-admin order.
            parent = db.get(AuthSession, active.auth_session_id, populate_existing=True) if active.auth_session_id else None
            reason = impersonation_invalidation_reason(db, active, admin, parent)
            if reason is None:
                raise ImpersonationError(409, ACTIVE_CONFLICT)
            finish_impersonation(db, active, status=ImpersonationStatus.EXPIRED, reason=reason)
            db.flush()

        target = eligible_impersonation_target(db, school_id, data.target_account_id)
        if target is None or target.id == admin.id:
            raise ImpersonationError(404, "Eligible account not found.")
        # The authenticated tab's row stays locked through target validation.
        parent = db.get(AuthSession, context.auth_session_id, populate_existing=True)
        started_at = db.scalar(select(func.clock_timestamp()))
        if parent is None or parent.account_id != admin.id or parent.revoked_at is not None or parent.expires_at <= started_at:
            raise ImpersonationError(401, "Authentication required.")
        session = ImpersonationSession(
            id=session_id,
            auth_session_id=context.auth_session_id,
            school_id=school_id,
            admin_account_id=admin.id,
            target_account_id=target.id,
            reason=data.reason,
            status=ImpersonationStatus.ACTIVE,
            started_at=started_at,
        )
        db.add(session)
        db.flush()
        db.add(AuditLog(
            actor_account_id=admin.id,
            represented_account_id=target.id,
            impersonation_session_id=session.id,
            action=STARTED,
            entity_type="impersonation_session",
            entity_id=str(session.id),
            occurred_at=started_at,
        ))
        return _state(session, target)


def current_impersonation(db: Session, context: AuthenticationContext) -> ImpersonationState:
    with _operation(db):
        school_id = require_actual_admin_school(context)
        query = select(ImpersonationSession).where(*_for_tab(context, school_id))
        if context.impersonation_session_id is not None:
            query = query.where(ImpersonationSession.id == context.impersonation_session_id)
        else:
            query = query.order_by(ImpersonationSession.started_at.desc(), ImpersonationSession.id.desc()).limit(1)
        session = db.scalar(query.execution_options(populate_existing=True))
        return _state(session, _target_for_state(db, session)) if session is not None else ImpersonationState()


def end_impersonation(
    db: Session, context: AuthenticationContext, data: EndImpersonationRequest,
) -> ImpersonationState:
    with _operation(db, write=True):
        school_id = require_actual_admin_school(context)
        session = db.scalar(select(ImpersonationSession).where(
            ImpersonationSession.id == data.impersonation_session_id, *_for_tab(context, school_id),
        ).with_for_update().execution_options(populate_existing=True))
        if session is None:
            raise ImpersonationError(404, "Impersonation session not found.")
        if session.status == ImpersonationStatus.ACTIVE:
            finish_impersonation(db, session, status=ImpersonationStatus.ENDED, reason="ADMIN_ENDED")
            if data.reason is not None:
                session.end_reason = data.reason
        return _state(session, _target_for_state(db, session))


def view_published_activities(db: Session, context: AuthenticationContext, limit: int) -> list[PublishedActivity]:
    with _operation(db, write=True):
        school_id = require_publications_viewer(context)
        try:
            items = [PublishedActivity.model_validate(row) for row in list_published_activities(db, context.effective_account, limit)]
        except ActivityError as exc:
            raise ImpersonationError(exc.status_code, "Published activities are unavailable.") from exc
        db.add(AuditLog(
            actor_account_id=context.actual_account.id,
            represented_account_id=context.effective_account.id,
            impersonation_session_id=context.impersonation_session_id,
            action=PUBLICATIONS_VIEWED,
            entity_type="school_activity_feed",
            entity_id=str(school_id),
            occurred_at=db.scalar(select(func.clock_timestamp())),
        ))
        return items
