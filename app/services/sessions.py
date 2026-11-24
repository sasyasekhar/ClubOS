
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


class InvalidSession(Exception):
    pass


class ImpersonationRestricted(Exception):
    pass


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


def utcnow():
    return datetime.now(timezone.utc)


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
        expires_at=utcnow() + IDLE_TIMEOUT,
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


def _lock_actual_account(db: Session, account_id: UUID) -> Account | None:
    # NO KEY UPDATE serializes this administrator's tabs while remaining
    # compatible with foreign-key checks by ordinary business operations.
    return db.scalar(
        select(Account).where(Account.id == account_id)
        .with_for_update(key_share=True).execution_options(populate_existing=True)
    )


def _parent_invalidation_reason(parent: AuthSession | None) -> str | None:
    if parent is None or parent.revoked_at is not None:
        return "PARENT_SESSION_REVOKED"
    if parent.expires_at <= utcnow():
        return "PARENT_SESSION_EXPIRED"
    return None


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
) -> tuple[str | None, UUID | None, Account | None]:
    reason = _parent_invalidation_reason(parent)
    if reason is not None:
        return reason, None, None
    if parent.id != impersonation.auth_session_id or parent.account_id != impersonation.admin_account_id:
        return "PARENT_SESSION_REVOKED", None, None

    school_id = _lock_admin_school(db, admin)
    if school_id is None or school_id != impersonation.school_id:
        return "ADMIN_AUTHORIZATION_LOST", school_id, None

    target = eligible_impersonation_target(db, school_id, impersonation.target_account_id)
    if target is None:
        return "TARGET_NO_LONGER_ELIGIBLE", school_id, None
    # A live parent can reach its deadline while a school/account lock waits.
    return _parent_invalidation_reason(parent), school_id, target


def impersonation_invalidation_reason(
    db: Session, impersonation: ImpersonationSession, actual_account: Account, parent_session: AuthSession | None,
) -> str | None:
    reason, _, _ = _validate_impersonation(db, impersonation, actual_account, parent_session)
    return reason


def finish_impersonation(
    db: Session, impersonation: ImpersonationSession, *, status: ImpersonationStatus, reason: str,
) -> None:
    """Stage a transition and audit while the caller holds the impersonation row lock."""
    if impersonation.status != ImpersonationStatus.ACTIVE:
        return

    ended_at = db.scalar(select(func.clock_timestamp()))
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


def _expire_bound_impersonation(db: Session, parent: AuthSession, reason: str) -> None:
    if _bound_impersonation(db, parent) is None:
        return
    _lock_actual_account(db, parent.account_id)
    impersonation = _bound_impersonation(db, parent, lock=True)
    if impersonation is not None:
        finish_impersonation(db, impersonation, status=ImpersonationStatus.EXPIRED, reason=reason)
        db.commit()


def find_session(db: Session, cookie_token: str | None, tab_key: str | None) -> AuthSession:
    auth_session = _lock_parent_session(db, cookie_token, tab_key)
    if auth_session is None:
        raise InvalidSession()
    reason = _parent_invalidation_reason(auth_session)
    if reason is not None:
        _expire_bound_impersonation(db, auth_session, reason)
        raise InvalidSession()
    return auth_session


def authentication_context(
    db: Session, cookie_token: str | None, tab_key: str | None, *, lock_admin: bool = False, renew: bool = True,
) -> AuthenticationContext:
    if lock_admin:
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
    parent = find_session(db, cookie_token, tab_key)
    account = db.get(Account, parent.account_id, populate_existing=True)
    if not _account_can_authenticate(db, account):
        _expire_bound_impersonation(db, parent, "ADMIN_AUTHORIZATION_LOST")
        raise InvalidSession()

    reason = _parent_invalidation_reason(parent)
    if reason is not None:
        _expire_bound_impersonation(db, parent, reason)
        raise InvalidSession()

    now = utcnow()
    if renew and parent.expires_at < now + IDLE_TIMEOUT - timedelta(minutes=2):
        if account.account_type == AccountType.SCHOOL_ADMIN:
            # A competing tab checks the earlier parent's idle deadline under
            # this lock. Serialize renewal before it can use that deadline.
            account = _lock_actual_account(db, parent.account_id)
            if not _account_can_authenticate(db, account):
                _expire_bound_impersonation(db, parent, "ADMIN_AUTHORIZATION_LOST")
                raise InvalidSession()
            now = utcnow()
            if parent.expires_at <= now:
                _expire_bound_impersonation(db, parent, "PARENT_SESSION_EXPIRED")
                raise InvalidSession()
        parent.expires_at = now + IDLE_TIMEOUT
        db.commit()
        # Renewal releases locks. Begin authorization again from the parent,
        # so logout or an impersonation transition can win before admission.
        return authentication_context(db, cookie_token, tab_key, lock_admin=lock_admin, renew=False)

    impersonation = _bound_impersonation(db, parent)
    if impersonation is None and (not lock_admin or account.account_type != AccountType.SCHOOL_ADMIN):
        if _parent_invalidation_reason(parent) is not None:
            raise InvalidSession()
        return AuthenticationContext(account, account, parent.id)

    account = _lock_actual_account(db, parent.account_id)
    impersonation = _bound_impersonation(db, parent, lock=True)
    if impersonation is None:
        if not _account_can_authenticate(db, account):
            raise InvalidSession()
        school_id = _lock_admin_school(db, account)
        if _parent_invalidation_reason(parent) is not None:
            raise InvalidSession()
        return AuthenticationContext(account, account, parent.id, authorized_school_id=school_id)

    reason, school_id, target = _validate_impersonation(db, impersonation, account, parent)
    if reason is None:
        return AuthenticationContext(account, target, parent.id, impersonation.id, school_id)

    finish_impersonation(db, impersonation, status=ImpersonationStatus.EXPIRED, reason=reason)
    db.commit()
    # Retain a fail-closed marker for this request. A valid next request may
    # use ordinary permissions again after the impersonation has ended.
    context = authentication_context(db, cookie_token, tab_key, lock_admin=lock_admin, renew=False)
    return replace(context, impersonation_invalidated=True)


def current_account(db: Session, cookie_token: str | None, tab_key: str | None) -> Account:
    context = authentication_context(db, cookie_token, tab_key)
    if context.is_impersonating or context.impersonation_invalidated:
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
            reason = _parent_invalidation_reason(parent)
            finish_impersonation(
                db, impersonation, status=ImpersonationStatus.EXPIRED if reason else ImpersonationStatus.ENDED,
                reason=reason or "LOGOUT",
            )

    if parent.revoked_at is None:
        parent.revoked_at = utcnow()
    db.commit()

