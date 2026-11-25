from datetime import timedelta
from hashlib import sha256

from sqlalchemy import BigInteger, func, literal, select, text
from sqlalchemy.orm import Session

from app.models.enums import LoginOutcome
from app.models.login_history import LoginHistory
from app.services.sessions import database_now


WINDOW_SECONDS = 300
CLIENT_FAILURE_LIMIT = 30
IDENTIFIER_FAILURE_LIMIT = 10


class LoginThrottled(Exception):
    pass


def lock_login_attempt(db: Session, identifier: str, client_host: str) -> tuple[str, str]:
    """Hold credential-scope locks until the success or failure has committed."""
    client = sha256(f"clubos:login:client:{client_host}".encode()).hexdigest()
    identity = sha256(f"clubos:login:identifier:{identifier.strip().lower()}".encode()).hexdigest()
    lock_ids = {int.from_bytes(bytes.fromhex(key[:16]), "big", signed=True) for key in (client, identity)}
    db.execute(text("SET LOCAL lock_timeout = '5s'"))
    for lock_id in sorted(lock_ids):
        db.execute(select(func.pg_advisory_xact_lock(literal(lock_id, BigInteger()))))
    since = database_now(db) - timedelta(seconds=WINDOW_SECONDS)
    for scope, value, maximum in (
        (LoginHistory.client_key, client, CLIENT_FAILURE_LIMIT),
        (LoginHistory.identifier_key, identity, IDENTIFIER_FAILURE_LIMIT),
    ):
        failure = db.scalar(select(LoginHistory.id).where(
            LoginHistory.outcome == LoginOutcome.FAILED, scope == value, LoginHistory.occurred_at > since,
        ).order_by(LoginHistory.occurred_at.desc()).offset(maximum - 1).limit(1))
        if failure is not None:
            raise LoginThrottled()
    return client, identity
