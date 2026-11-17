
from sqlalchemy import select, func, or_
from sqlalchemy.orm import Session

from app.models.account import Account
from app.security.password import hash_password, verify_password


class InvalidCredentials(Exception):
    pass


# Prevent a noticeably faster response for unknown usernames.
_DUMMY_HASH = hash_password("clubos-invalid-account-placeholder")


def authenticate_account(
    db: Session,
    identifier: str,
    password: str,
) -> Account:
    normalized = identifier.strip().lower()

    if not normalized or not password:
        raise InvalidCredentials()

    accounts = db.scalars(
        select(Account)
        .where(
            or_(
                func.lower(Account.username) == normalized,
                func.lower(Account.email) == normalized,
            )
        )
        .limit(2)
    ).all()

    if len(accounts) != 1:
        verify_password(password, _DUMMY_HASH)
        raise InvalidCredentials()

    account = accounts[0]

    valid_password = verify_password(
        password, account.password_hash
    )

    if not valid_password or not account.is_active:
        raise InvalidCredentials()

    return account

