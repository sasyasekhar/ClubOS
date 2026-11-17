
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.school import School
from app.models.auth_session import AuthSession
from app.models.login_history import LoginHistory
from app.models.enums import LoginOutcome


IDLE_TIMEOUT = timedelta(hours=12)


class InvalidSession(Exception):
    pass


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


def find_session(db: Session, cookie_token: str, tab_key: str):
    if not cookie_token or not tab_key:
        raise InvalidSession()

    auth_session = db.scalar(
        select(AuthSession)
        .where(
            AuthSession.token_hash == token_hash(cookie_token, tab_key)
        )
        .with_for_update()
    )

    if (
        auth_session is None
        or auth_session.revoked_at is not None
        or auth_session.expires_at <= utcnow()
    ):
        raise InvalidSession()

    return auth_session


def current_account(db: Session, cookie_token: str, tab_key: str):
    auth_session = find_session(db, cookie_token, tab_key)

    account = db.get(Account, auth_session.account_id)
    if account is None or not account.is_active:
        raise InvalidSession()

    if account.school_id is not None:
        school = db.get(School, account.school_id)
        if school is None or not school.is_active:
            raise InvalidSession()

    now = utcnow()

    # Reduce DB writes: renew at most once every two minutes.
    if auth_session.expires_at < now + IDLE_TIMEOUT - timedelta(minutes=2):
        auth_session.expires_at = now + IDLE_TIMEOUT
        db.commit()

    return account


def revoke_session(db: Session, cookie_token: str, tab_key: str):
    if not cookie_token or not tab_key:
        return

    auth_session = db.scalar(
        select(AuthSession)
        .where(
            AuthSession.token_hash == token_hash(cookie_token, tab_key)
        )
        .with_for_update()
    )

    if auth_session and auth_session.revoked_at is None:
        auth_session.revoked_at = utcnow()
        db.commit()

