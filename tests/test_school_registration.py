
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.database import engine
from app.models.account import Account
from app.models.school import School
from app.models.school_admin import SchoolAdmin
from app.models.enums import AccountType
from app.schemas.auth import SchoolRegistration
from app.security.password import verify_password
from app.services import school_registration as service


@pytest.fixture
def db():
    if engine.url.database != "clubos_test":
        pytest.fail("Tests require the clubos_test database")

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


def make_registration(**overrides):
    unique = uuid.uuid4().hex[:12]

    data = {
        "school_name": f"Test School {unique}",
        "admin_full_name": "Test Administrator",
        "username": f"admin_{unique}",
        "email": f"{unique}@example.com",
        "password": "SecureTestPassword123!",
    }
    data.update(overrides)
    return SchoolRegistration(**data)


def test_school_registration_success(db):
    data = make_registration()
    result = service.register_school(db, data)

    school = db.get(School, result["school_id"])
    account = db.get(Account, result["admin_account_id"])
    admin = db.get(SchoolAdmin, result["school_id"])

    assert school is not None
    assert school.school_code == result["school_code"]
    assert account.school_id == school.id
    assert account.account_type == AccountType.SCHOOL_ADMIN
    assert verify_password(data.password, account.password_hash)
    assert admin.account_id == account.id


def test_duplicate_username_rolls_back(db):
    first = make_registration()
    service.register_school(db, first)

    second = make_registration(username=first.username.upper())

    with pytest.raises(service.RegistrationConflict):
        service.register_school(db, second)

    assert db.scalar(
        select(School).where(School.name == second.school_name)
    ) is None


def test_failure_rolls_back_all_records(db, monkeypatch):
    data = make_registration()
    code = "CLB-" + uuid.uuid4().hex[:12].upper()

    monkeypatch.setattr(
        service, "generate_school_code", lambda: code
    )

    def simulate_failure(**kwargs):
        raise RuntimeError("Simulated administrator creation failure")

    monkeypatch.setattr(
        service, "SchoolAdmin", simulate_failure
    )

    with pytest.raises(RuntimeError):
        service.register_school(db, data)

    assert db.scalar(
        select(School).where(School.school_code == code)
    ) is None

    assert db.scalar(
        select(Account).where(Account.username == data.username)
    ) is None
