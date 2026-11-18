
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.main import app
from app.config.database import engine, get_db
from app.routes.auth import require_account
from app.models.account import Account
from app.models.academic_year import AcademicYear
from app.models.audit_log import AuditLog
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.enums import (
    AccountType,
    AcademicYearStatus,
    ClubStatus,
    ClubRole,
    MembershipStatus,
)
from app.schemas.auth import SchoolRegistration, MemberRegistration
from app.services.school_registration import register_school
from app.services.member_registration import register_member


@pytest.fixture
def env():
    if engine.url.database != "clubos_test":
        pytest.fail("Use the clubos_test database")

    with engine.connect() as connection:
        transaction = connection.begin()
        db = Session(
            bind=connection,
            join_transaction_mode="create_savepoint",
        )

        suffix = uuid.uuid4().hex[:10]
        school = register_school(db, SchoolRegistration(
            school_name=f"Club School {suffix}",
            admin_full_name="Administrator",
            username=f"admin_{suffix}",
            email=f"admin_{suffix}@example.com",
            password="SecureAdminPassword123!",
        ))

        def make_member(role, name):
            result = register_member(db, MemberRegistration(
                school_code=school["school_code"],
                full_name=name,
                username=f"{name.lower()}_{suffix}",
                email=f"{name.lower()}_{suffix}@example.com",
                password="SecureMemberPassword123!",
                account_type=role,
            ))
            return db.get(Account, result["account_id"])

        student = make_member(AccountType.STUDENT, "Student")
        teacher = make_member(AccountType.TEACHER, "Teacher")
        admin = db.get(Account, school["admin_account_id"])

        year = AcademicYear(
            school_id=school["school_id"],
            name="2026-2027",
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
            status=AcademicYearStatus.ACTIVE,
        )
        db.add(year)
        db.commit()

        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[require_account] = lambda: student

        try:
            with TestClient(app) as client:
                yield {
                    "client": client,
                    "db": db,
                    "student": student,
                    "teacher": teacher,
                    "admin": admin,
                    "year": year,
                }
        finally:
            app.dependency_overrides.clear()
            db.close()
            transaction.rollback()


def payload(env):
    return {
        "name": "Robotics Club",
        "description": "Engineering projects",
        "category": "STEM",
        "teacher_account_id": str(env["teacher"].id),
    }


def submit(env, key=None, data=None):
    return env["client"].post(
        "/clubs",
        json=data if data is not None else payload(env),
        headers={"Idempotency-Key": str(key or uuid.uuid4())},
    )


def test_student_can_request_club(env):
    response = submit(env)
    assert response.status_code == 201, response.text

    club = env["db"].get(Club, response.json()["id"])
    assert club.status == ClubStatus.PENDING
    assert club.school_id == env["student"].school_id
    assert club.academic_year_id == env["year"].id

    membership = env["db"].scalar(
        select(ClubMembership).where(
            ClubMembership.club_id == club.id
        )
    )
    assert membership.account_id == env["teacher"].id
    assert membership.role == ClubRole.TEACHER_REP
    assert membership.status == MembershipStatus.PENDING


def test_duplicate_submission_creates_one_club(env):
    key = uuid.uuid4()

    first = submit(env, key)
    second = submit(env, key)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    club_id = first.json()["id"]

    assert env["db"].scalar(
        select(func.count()).select_from(Club).where(
            Club.id == uuid.UUID(club_id)
        )
    ) == 1

    assert env["db"].scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.entity_id == club_id,
            AuditLog.action == "CLUB_REGISTRATION_REQUESTED",
        )
    ) == 1


def test_reusing_key_with_different_payload_fails(env):
    key = uuid.uuid4()
    assert submit(env, key).status_code == 201

    changed = payload(env)
    changed["name"] = "Another Club"

    assert submit(env, key, changed).status_code == 409


def test_non_teacher_cannot_be_selected(env):
    data = payload(env)
    data["teacher_account_id"] = str(env["student"].id)

    assert submit(env, data=data).status_code == 404


def test_admin_cannot_submit_student_club_request(env):
    app.dependency_overrides[require_account] = (
        lambda: env["admin"]
    )
    assert submit(env).status_code == 403


def test_active_academic_year_required(env):
    env["year"].status = AcademicYearStatus.CLOSED
    env["db"].commit()

    assert submit(env).status_code == 409


def test_idempotency_key_is_required(env):
    response = env["client"].post("/clubs", json=payload(env))
    assert response.status_code == 422
