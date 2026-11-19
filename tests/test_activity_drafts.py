
from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.main import app
from app.config.database import engine, get_db
from app.routes.auth import require_account

from app.models.school import School
from app.models.account import Account
from app.models.academic_year import AcademicYear
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.activity import Activity
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog

from app.models.enums import (
    AccountType,
    AcademicYearStatus,
    ClubStatus,
    ClubRole,
    MembershipStatus,
    ActivityRevisionStatus,
)


@pytest.fixture
def env():
    if engine.url.database != "clubos_test":
        pytest.fail("Use clubos_test database")

    with engine.connect() as connection:
        transaction = connection.begin()
        db = Session(
            bind=connection,
            join_transaction_mode="create_savepoint",
        )

        suffix = uuid4().hex[:12]

        school = School(
            name=f"Activity School {suffix}",
            school_code=f"CLB-{suffix.upper()}",
        )
        db.add(school)
        db.flush()

        def account(role, prefix):
            user = Account(
                school_id=school.id,
                account_type=role,
                full_name=prefix,
                username=f"{prefix}_{suffix}",
                email=f"{prefix}_{suffix}@example.com",
                password_hash="test-only",
            )
            db.add(user)
            db.flush()
            return user

        student = account(AccountType.STUDENT, "student")
        other = account(AccountType.STUDENT, "other")
        teacher = account(AccountType.TEACHER, "teacher")

        year = AcademicYear(
            school_id=school.id,
            name="2026-2027",
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
            status=AcademicYearStatus.ACTIVE,
        )
        db.add(year)
        db.flush()

        club = Club(
            school_id=school.id,
            academic_year_id=year.id,
            created_by=student.id,
            name="Robotics Club",
            category="STEM",
            status=ClubStatus.ACTIVE,
        )
        db.add(club)
        db.flush()

        db.add(ClubMembership(
            club_id=club.id,
            account_id=student.id,
            role=ClubRole.STUDENT_REP,
            status=MembershipStatus.ACTIVE,
            start_date=date.today(),
        ))
        db.commit()

        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[require_account] = lambda: student

        try:
            with TestClient(app) as client:
                yield {
                    "client": client,
                    "db": db,
                    "club": club,
                    "student": student,
                    "other": other,
                    "teacher": teacher,
                    "year": year,
                }
        finally:
            app.dependency_overrides.clear()
            db.close()
            transaction.rollback()


def draft_data():
    return {
        "title": "Robotics Workshop",
        "activity_date": "2026-11-25",
        "description": "Introduction to robotics",
        "category": "STEM",
    }


def create(env, key=None, data=None):
    return env["client"].post(
        f"/clubs/{env['club'].id}/activities",
        headers={
            "Idempotency-Key": str(key or uuid4())
        },
        json=data if data is not None else draft_data(),
    )


def test_create_activity_draft(env):
    response = create(env)

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "DRAFT"

    activity = env["db"].get(
        Activity, response.json()["activity_id"]
    )
    revision = env["db"].get(
        ActivityRevision, response.json()["revision_id"]
    )

    assert activity.club_id == env["club"].id
    assert activity.created_by == env["student"].id
    assert revision.revision_number == 1
    assert revision.status == ActivityRevisionStatus.DRAFT
    assert revision.is_published is False


def test_duplicate_draft_is_idempotent(env):
    key = uuid4()

    first = create(env, key)
    second = create(env, key)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()

    assert env["db"].scalar(
        select(func.count()).select_from(Activity).where(
            Activity.id == first.json()["activity_id"]
        )
    ) == 1

    assert env["db"].scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "ACTIVITY_DRAFT_CREATED",
            AuditLog.entity_id == first.json()["activity_id"],
        )
    ) == 1


def test_reused_key_with_different_content_fails(env):
    key = uuid4()

    assert create(env, key).status_code == 201

    changed = draft_data()
    changed["title"] = "Different Workshop"

    assert create(env, key, changed).status_code == 409


def test_non_representative_cannot_create(env):
    app.dependency_overrides[require_account] = (
        lambda: env["other"]
    )

    assert create(env).status_code == 403


def test_teacher_cannot_create_draft(env):
    app.dependency_overrides[require_account] = (
        lambda: env["teacher"]
    )

    assert create(env).status_code == 403


def test_inactive_club_rejected(env):
    env["club"].status = ClubStatus.ARCHIVED
    env["db"].commit()

    assert create(env).status_code == 409


def test_closed_academic_year_rejected(env):
    env["year"].status = AcademicYearStatus.CLOSED
    env["db"].commit()

    assert create(env).status_code == 409


def test_missing_idempotency_key(env):
    response = env["client"].post(
        f"/clubs/{env['club'].id}/activities",
        json=draft_data(),
    )

    assert response.status_code == 422
