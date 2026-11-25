
import hashlib
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.auth_session import AuthSession
from app.models.enums import AccountType, ImpersonationStatus, LoginOutcome
from app.models.impersonation_session import ImpersonationSession
from app.models.login_history import LoginHistory
from app.models.school import School
from app.models.school_admin import SchoolAdmin


IDLE_TIMEOUT = timedelta(hours=12)
IMPERSONATION_MAX_DURATION = timedelta(minutes=15)


class InvalidSession(Exception):
    pass


class ImpersonationRestricted(Exception):
    def __init__(self, detail: str = "End impersonation before using this operation."):
        super().__init__(detail)


@dataclass(frozen=True)
class AuthenticationContext:
    actual_account: Account
    effective_account: Account
    auth_session_id: UUID
    impersonation_session_id: UUID | None = None
    authorized_school_id: UUID | None = None
    impersonation_invalidated: bool = False

    @property
    def is_impersonating(self) -> bool:
        return self.impersonation_session_id is not None


def database_now(db: Session) -> datetime:
    return db.execute(select(func.clock_timestamp())).scalar_one().astimezone(timezone.utc)


def token_hash(cookie_token, tab_key):
    value = f"{cookie_token}:{tab_key}"
    return hashlib.sha256(value.encode()).hexdigest()


def record_failed_login(db: Session, identifier: str):
    db.add(LoginHistory(
        attempted_identifier=identifier[:255],
        outcome=LoginOutcome.FAILED,
    ))
    db.commit()


def create_session(db: Session, account: Account, identifier: str):
    cookie_token = secrets.token_urlsafe(32)
    tab_key = secrets.token_urlsafe(32)

    db.add(AuthSession(
        account_id=account.id,
        token_hash=token_hash(cookie_token, tab_key),
        expires_at=database_now(db) + IDLE_TIMEOUT,
    ))

    db.add(LoginHistory(
        account_id=account.id,
        attempted_identifier=identifier[:255],
        outcome=LoginOutcome.SUCCESS,
    ))

    db.commit()
    return cookie_token, tab_key


def _lock_parent_session(db: Session, cookie_token: str | None, tab_key: str | None) -> AuthSession | None:
    if not cookie_token or not tab_key:
        return None

    return db.scalar(
        select(AuthSession)
        .where(AuthSession.token_hash == token_hash(cookie_token, tab_key))
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _bound_impersonation(db: Session, parent: AuthSession, *, lock: bool = False) -> ImpersonationSession | None:
    query = select(ImpersonationSession).where(
        ImpersonationSession.auth_session_id == parent.id,
        ImpersonationSession.admin_account_id == parent.account_id,
        ImpersonationSession.status == ImpersonationStatus.ACTIVE,
    )
    if lock:
        query = query.with_for_update()
    return db.scalar(query.execution_options(populate_existing=True))


def _observed_impersonation_id(db: Session, cookie_token: str | None, tab_key: str | None) -> UUID | None:
    if not cookie_token or not tab_key:
        return None
    # Observe the tab before waiting for its parent lock. A concurrent terminal
    # transition must not turn an in-flight impersonated request into a direct one.
    return db.scalar(
        select(ImpersonationSession.id)
        .join(AuthSession, AuthSession.id == ImpersonationSession.auth_session_id)
        .where(AuthSession.token_hash == token_hash(cookie_token, tab_key), ImpersonationSession.status == ImpersonationStatus.ACTIVE)
    )


def _lock_actual_account(db: Session, account_id: UUID) -> Account | None:
    # NO KEY UPDATE serializes this administrator's tabs while remaining
    # compatible with foreign-key checks by ordinary business operations.
    return db.scalar(
        select(Account).where(Account.id == account_id)
        .with_for_update(key_share=True).execution_options(populate_existing=True)
    )


def _parent_invalidation_reason(parent: AuthSession | None, checked_at: datetime) -> str | None:
    if parent is None or parent.revoked_at is not None:
        return "PARENT_SESSION_REVOKED"
    if parent.expires_at.astimezone(timezone.utc) <= checked_at:
        return "PARENT_SESSION_EXPIRED"
    return None


def impersonation_deadline(impersonation: ImpersonationSession, parent: AuthSession) -> datetime:
    assert impersonation.expires_at is not None
    return min(impersonation.expires_at.astimezone(timezone.utc), parent.expires_at.astimezone(timezone.utc))


def impersonation_time_invalidation_reason(
    impersonation: ImpersonationSession, parent: AuthSession | None, checked_at: datetime,
) -> str | None:
    if parent is None or parent.revoked_at is not None:
        return "PARENT_SESSION_REVOKED"
    deadline = impersonation_deadline(impersonation, parent)
    if checked_at.astimezone(timezone.utc) < deadline:
        return None
    maximum_deadline = impersonation.started_at.astimezone(timezone.utc) + IMPERSONATION_MAX_DURATION
    if deadline == parent.expires_at.astimezone(timezone.utc) or deadline < maximum_deadline:
        return "PARENT_SESSION_EXPIRED"
    return "IMPERSONATION_TIMEOUT"


def _account_can_authenticate(db: Session, account: Account | None) -> bool:
    return account is not None and account.is_active and (
        account.school_id is None
        or bool(db.scalar(select(School.is_active).where(School.id == account.school_id)))
    )


def _lock_admin_school(db: Session, admin: Account | None) -> UUID | None:
    if admin is None or admin.account_type != AccountType.SCHOOL_ADMIN or not admin.is_active or admin.school_id is None:
        return None
    return db.scalar(
        select(School.id)
        .join(SchoolAdmin, SchoolAdmin.school_id == School.id)
        .where(School.id == admin.school_id, School.is_active, SchoolAdmin.account_id == admin.id)
        .with_for_update(read=True, of=(School, SchoolAdmin))
    )


def eligible_impersonation_target(db: Session, school_id: UUID, target_account_id: UUID) -> Account | None:
    return db.scalar(
        select(Account).where(
            Account.id == target_account_id,
            Account.school_id == school_id,
            Account.account_type.in_((AccountType.STUDENT, AccountType.TEACHER)),
            Account.is_active,
        ).with_for_update(read=True).execution_options(populate_existing=True)
    )


def _validate_impersonation(
    db: Session, impersonation: ImpersonationSession, admin: Account | None, parent: AuthSession | None,
) -> tuple[str | None, UUID | None, Account | None, datetime]:
    checked_at = database_now(db)
    reason = impersonation_time_invalidation_reason(impersonation, parent, checked_at)
    if reason is not None:
        return reason, None, None, checked_at
    if parent.id != impersonation.auth_session_id or parent.account_id != impersonation.admin_account_id:
        return "PARENT_SESSION_REVOKED", None, None, checked_at

    school_id = _lock_admin_school(db, admin)
    if school_id is None or school_id != impersonation.school_id:
        return "ADMIN_AUTHORIZATION_LOST", school_id, None, database_now(db)

    target = eligible_impersonation_target(db, school_id, impersonation.target_account_id)
    checked_at = database_now(db)
    if target is None:
        return "TARGET_NO_LONGER_ELIGIBLE", school_id, None, checked_at
    # Either cutoff can pass while the school or target row is locked elsewhere.
    reason = impersonation_time_invalidation_reason(impersonation, parent, checked_at)
    return reason, school_id, target, checked_at


def impersonation_invalidation_reason(
    db: Session, impersonation: ImpersonationSession, actual_account: Account, parent_session: AuthSession | None,
) -> str | None:
    reason, _, _, _ = _validate_impersonation(db, impersonation, actual_account, parent_session)
    return reason


def finish_impersonation(
    db: Session, impersonation: ImpersonationSession, *, status: ImpersonationStatus, reason: str,
    ended_at: datetime | None = None,
) -> None:
    """Stage a transition and audit while the caller holds the impersonation row lock."""
    if impersonation.status != ImpersonationStatus.ACTIVE:
        return

    ended_at = ended_at if ended_at is not None else database_now(db)
    impersonation.status = status
    impersonation.ended_at = ended_at
    impersonation.end_reason = reason
    db.add(AuditLog(
        actor_account_id=impersonation.admin_account_id,
        represented_account_id=impersonation.target_account_id,
        impersonation_session_id=impersonation.id,
        action="IMPERSONATION_ENDED" if status == ImpersonationStatus.ENDED else "IMPERSONATION_INVALIDATED",
        entity_type="impersonation_session",
        entity_id=str(impersonation.id),
        details={"reason": reason},
        occurred_at=ended_at,
    ))


def expire_impersonation(
    db: Session, impersonation: ImpersonationSession, parent: AuthSession | None, reason: str,
    *, checked_at: datetime | None = None,
) -> None:
    if parent is not None:
        impersonation.expires_at = impersonation_deadline(impersonation, parent)
    finish_impersonation(db, impersonation, status=ImpersonationStatus.EXPIRED, reason=reason, ended_at=checked_at)


def check_impersonation_deadline(
    db: Session, context: AuthenticationContext, checked_at: datetime | None = None,
) -> datetime:
    """Recheck time under the authentication locks after waiting on business rows."""
    assert context.is_impersonating and not context.impersonation_invalidated
    impersonation = db.get(ImpersonationSession, context.impersonation_session_id)
    parent = db.get(AuthSession, context.auth_session_id)
    assert impersonation is not None and impersonation.status == ImpersonationStatus.ACTIVE
    checked_at = checked_at if checked_at is not None else database_now(db)
    reason = impersonation_time_invalidation_reason(impersonation, parent, checked_at)
    if reason is not None:
        # Callers reach this check before staging any business change or read audit.
        expire_impersonation(db, impersonation, parent, reason, checked_at=checked_at)
        db.commit()
        raise ImpersonationRestricted("The impersonation is no longer active.")
    return checked_at


def _expire_bound_impersonation(db: Session, parent: AuthSession, reason: str) -> None:
    if _bound_impersonation(db, parent) is None:
        return
    _lock_actual_account(db, parent.account_id)
    impersonation = _bound_impersonation(db, parent, lock=True)
    if impersonation is not None:
        checked_at = database_now(db)
        reason = impersonation_time_invalidation_reason(impersonation, parent, checked_at) or reason
        expire_impersonation(db, impersonation, parent, reason, checked_at=checked_at)
        db.commit()


def find_session(db: Session, cookie_token: str | None, tab_key: str | None) -> AuthSession:
    auth_session = _lock_parent_session(db, cookie_token, tab_key)
    if auth_session is None:
        raise InvalidSession()
    reason = _parent_invalidation_reason(auth_session, database_now(db))
    if reason is not None:
        _expire_bound_impersonation(db, auth_session, reason)
        raise InvalidSession()
    return auth_session


def authentication_context(
    db: Session, cookie_token: str | None, tab_key: str | None, *, lock_admin: bool = False, renew: bool = True,
) -> AuthenticationContext:
    if lock_admin:
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
    observed_impersonation_id = _observed_impersonation_id(db, cookie_token, tab_key)
    parent = find_session(db, cookie_token, tab_key)
    account = db.get(Account, parent.account_id, populate_existing=True)
    if not _account_can_authenticate(db, account):
        _expire_bound_impersonation(db, parent, "ADMIN_AUTHORIZATION_LOST")
        raise InvalidSession()

    impersonation = _bound_impersonation(db, parent)
    now = database_now(db)
    renew_at = parent.expires_at.astimezone(timezone.utc) - IDLE_TIMEOUT + timedelta(minutes=2)
    school_id = None
    target = account
    if impersonation is not None or (
        account.account_type == AccountType.SCHOOL_ADMIN and (lock_admin or (renew and now > renew_at))
    ):
        # Serialize lifecycle operations and ordinary renewal across the admin's tabs.
        account = _lock_actual_account(db, parent.account_id)
        impersonation = _bound_impersonation(db, parent, lock=True)
        if not _account_can_authenticate(db, account):
            _expire_bound_impersonation(db, parent, "ADMIN_AUTHORIZATION_LOST")
            raise InvalidSession()
        if impersonation is None:
            school_id = _lock_admin_school(db, account) if lock_admin else None
            target = account
            now = database_now(db)
        else:
            reason, school_id, target, now = _validate_impersonation(db, impersonation, account, parent)
            if reason is not None:
                expire_impersonation(db, impersonation, parent, reason, checked_at=now)
                db.commit()
                # A request that discovers invalidation cannot use ordinary permissions.
                context = authentication_context(db, cookie_token, tab_key, lock_admin=lock_admin, renew=False)
                return replace(context, impersonation_invalidated=True)

    reason = _parent_invalidation_reason(parent, now)
    if reason is not None:
        _expire_bound_impersonation(db, parent, reason)
        raise InvalidSession()

    impersonation_id = impersonation.id if impersonation is not None else None
    invalidated = observed_impersonation_id is not None and observed_impersonation_id != impersonation_id
    if renew and now > renew_at:
        if impersonation is not None:
            # Persist a shortened parent deadline before replacing it with a later one.
            impersonation.expires_at = impersonation_deadline(impersonation, parent)
        parent.expires_at = now + IDLE_TIMEOUT
        db.commit()
        # Reacquire the authentication locks before admitting the request.
        context = authentication_context(db, cookie_token, tab_key, lock_admin=lock_admin, renew=False)
        if invalidated or (impersonation_id is not None and context.impersonation_session_id != impersonation_id):
            return replace(context, impersonation_invalidated=True)
        return context

    return AuthenticationContext(
        account, target, parent.id, impersonation_id, school_id, impersonation_invalidated=invalidated,
    )


def current_account(db: Session, cookie_token: str | None, tab_key: str | None) -> Account:
    context = authentication_context(db, cookie_token, tab_key)
    if context.impersonation_invalidated:
        raise ImpersonationRestricted("The impersonation is no longer active.")
    if context.is_impersonating:
        raise ImpersonationRestricted()
    return context.actual_account


def revoke_session(db: Session, cookie_token: str | None, tab_key: str | None) -> None:
    if not cookie_token or not tab_key:
        return
    db.execute(text("SET LOCAL statement_timeout = '10s'"))
    parent = _lock_parent_session(db, cookie_token, tab_key)
    if parent is None:
        return

    if _bound_impersonation(db, parent) is not None:
        _lock_actual_account(db, parent.account_id)
        impersonation = _bound_impersonation(db, parent, lock=True)
        if impersonation is not None:
            now = database_now(db)
            reason = impersonation_time_invalidation_reason(impersonation, parent, now)
            if reason is not None:
                expire_impersonation(db, impersonation, parent, reason, checked_at=now)
            else:
                impersonation.expires_at = impersonation_deadline(impersonation, parent)
                finish_impersonation(db, impersonation, status=ImpersonationStatus.ENDED, reason="LOGOUT", ended_at=now)

    if parent.revoked_at is None:
        parent.revoked_at = database_now(db)
    db.commit()

