
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config.database import engine
from app.models.account import Account
from app.models.school import School
from app.models.enums import AccountType
from app.schemas.auth import (
    SchoolRegistration,
    MemberRegistration,
)
from app.security.password import verify_password
from app.services.school_registration import (
    register_school,
    RegistrationConflict,
)
from app.services.member_registration import (
    register_member,
    SchoolNotAvailable,
)


@pytest.fixture
def db():
    if engine.url.database != "clubos_test":
        pytest.fail("Use the clubos_test database")

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


def create_test_school(db):
    suffix = uuid.uuid4().hex[:12]

    return register_school(
        db,
        SchoolRegistration(
            school_name=f"School {suffix}",
            admin_full_name="Test Admin",
            username=f"admin_{suffix}",
            email=f"admin_{suffix}@example.com",
            password="SecureAdminPassword123!",
        ),
    )


def make_member(school_code, **overrides):
    suffix = uuid.uuid4().hex[:12]

    data = {
        "school_code": school_code,
        "full_name": "Test Member",
        "username": f"user_{suffix}",
        "email": f"user_{suffix}@example.com",
        "password": "SecureMemberPassword123!",
        "account_type": AccountType.STUDENT,
    }

    data.update(overrides)
    return MemberRegistration(**data)


@pytest.mark.parametrize(
    "role",
    [AccountType.STUDENT, AccountType.TEACHER],
)
def test_register_member(db, role):
    school = create_test_school(db)

    data = make_member(
        school["school_code"].lower(),
        account_type=role,
    )

    result = register_member(db, data)

    account = db.get(Account, result["account_id"])

    assert account is not None
    assert account.school_id == school["school_id"]
    assert account.account_type == role
    assert verify_password(data.password, account.password_hash)


def test_invalid_school_code(db):
    data = make_member("CLB-INVALID-CODE")

    with pytest.raises(SchoolNotAvailable):
        register_member(db, data)


def test_inactive_school(db):
    result = create_test_school(db)
    school = db.get(School, result["school_id"])

    school.is_active = False
    db.flush()

    data = make_member(result["school_code"])

    with pytest.raises(SchoolNotAvailable):
        register_member(db, data)


def test_duplicate_username(db):
    school = create_test_school(db)
    first = make_member(school["school_code"])

    register_member(db, first)

    second = make_member(
        school["school_code"],
        username=first.username.upper(),
    )

    with pytest.raises(RegistrationConflict):
        register_member(db, second)


def test_duplicate_email(db):
    school = create_test_school(db)
    first = make_member(school["school_code"])

    register_member(db, first)

    second = make_member(
        school["school_code"],
        email=str(first.email).upper(),
    )

    with pytest.raises(RegistrationConflict):
        register_member(db, second)


def test_privileged_role_rejected(db):
    school = create_test_school(db)

    with pytest.raises(ValidationError):
        make_member(
            school["school_code"],
            account_type=AccountType.SCHOOL_ADMIN,
        )

