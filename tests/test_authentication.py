
import uuid

import pytest
from sqlalchemy.orm import Session

from app.config.database import engine
from app.models.account import Account
from app.models.enums import AccountType
from app.schemas.auth import SchoolRegistration, MemberRegistration
from app.services.school_registration import register_school
from app.services.member_registration import register_member
from app.services.authentication import (
    authenticate_account,
    InvalidCredentials,
)


@pytest.fixture
def db():
    if engine.url.database != "clubos_test":
        pytest.fail("Tests require clubos_test")

    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(
            bind=connection,
            join_transaction_mode="create_savepoint",
        )

        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


@pytest.fixture
def member(db):
    suffix = uuid.uuid4().hex[:12]
    password = "SecureLoginPassword123!"

    school = register_school(
        db,
        SchoolRegistration(
            school_name=f"Login School {suffix}",
            admin_full_name="Administrator",
            username=f"admin_{suffix}",
            email=f"admin_{suffix}@example.com",
            password="SecureAdminPassword123!",
        ),
    )

    result = register_member(
        db,
        MemberRegistration(
            school_code=school["school_code"],
            full_name="Test Student",
            username=f"student_{suffix}",
            email=f"student_{suffix}@example.com",
            password=password,
            account_type=AccountType.STUDENT,
        ),
    )

    return db.get(Account, result["account_id"]), password


@pytest.mark.parametrize("field", ["username", "email"])
def test_login_valid_identifier(db, member, field):
    account, password = member

    identifier = getattr(account, field).upper()

    authenticated = authenticate_account(
        db, f" {identifier} ", password
    )

    assert authenticated.id == account.id


def test_login_wrong_password(db, member):
    account, _ = member

    with pytest.raises(InvalidCredentials):
        authenticate_account(
            db, account.username, "WrongPassword123!"
        )


def test_login_unknown_account(db):
    with pytest.raises(InvalidCredentials):
        authenticate_account(
            db, "unknown_account", "SomePassword123!"
        )


def test_login_inactive_account(db, member):
    account, password = member
    account.is_active = False
    db.flush()

    with pytest.raises(InvalidCredentials):
        authenticate_account(db, account.username, password)

