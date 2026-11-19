
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
from app.models.club_review import ClubReview
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


def review_request(env, club_id, decision, comments=None, key=None):
    return env["client"].post(
        f"/clubs/{club_id}/reviews",
        json={"decision": decision, "comments": comments},
        headers={"Idempotency-Key": str(key or uuid.uuid4())},
    )


def club_for_admin(env):
    response = submit(env)
    assert response.status_code == 201, response.text

    app.dependency_overrides[require_account] = (
        lambda: env["admin"]
    )
    return response.json()["id"]


@pytest.mark.parametrize(
    "decision,expected",
    [
        ("APPROVED", ClubStatus.ACTIVE),
        ("REJECTED", ClubStatus.REJECTED),
        ("CHANGES_REQUESTED", ClubStatus.CHANGES_REQUESTED),
    ],
)
def test_admin_review_decisions(env, decision, expected):
    club_id = club_for_admin(env)

    response = review_request(
        env, club_id, decision, "Review explanation"
    )

    assert response.status_code == 201, response.text
    assert env["db"].get(Club, uuid.UUID(club_id)).status == expected

    if decision == "APPROVED":
        rep = env["db"].scalar(
            select(ClubMembership).where(
                ClubMembership.club_id == uuid.UUID(club_id),
                ClubMembership.role == ClubRole.STUDENT_REP,
            )
        )
        assert rep is not None
        assert rep.account_id == env["student"].id
        assert rep.status == MembershipStatus.ACTIVE


def test_review_requires_explanation(env):
    club_id = club_for_admin(env)

    for decision in ("REJECTED", "CHANGES_REQUESTED"):
        response = review_request(env, club_id, decision)
        assert response.status_code == 422


def test_double_review_is_idempotent(env):
    club_id = club_for_admin(env)
    key = uuid.uuid4()

    first = review_request(env, club_id, "APPROVED", key=key)
    second = review_request(env, club_id, "APPROVED", key=key)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()

    assert env["db"].scalar(
        select(func.count()).select_from(ClubReview).where(
            ClubReview.club_id == uuid.UUID(club_id)
        )
    ) == 1


def test_second_review_rejected(env):
    club_id = club_for_admin(env)
    assert review_request(env, club_id, "APPROVED").status_code == 201
    assert review_request(env, club_id, "REJECTED", "No").status_code == 409


def test_student_cannot_review(env):
    club_id = club_for_admin(env)
    app.dependency_overrides[require_account] = (
        lambda: env["student"]
    )

    assert review_request(env, club_id, "APPROVED").status_code == 403


def test_cross_school_admin_cannot_review(env):
    club_id = club_for_admin(env)
    suffix = uuid.uuid4().hex[:10]

    other_school = register_school(
        env["db"],
        SchoolRegistration(
            school_name=f"Other School {suffix}",
            admin_full_name="Other Admin",
            username=f"other_admin_{suffix}",
            email=f"other_{suffix}@example.com",
            password="SecureOtherPassword123!",
        ),
    )

    other_admin = env["db"].get(
        Account, other_school["admin_account_id"]
    )

    app.dependency_overrides[require_account] = (
        lambda: other_admin
    )

    assert review_request(env, club_id, "APPROVED").status_code == 404

def club_needing_changes(env):
    club_id = club_for_admin(env)

    response = review_request(
        env,
        club_id,
        "CHANGES_REQUESTED",
        "Please improve the description.",
    )
    assert response.status_code == 201, response.text

    app.dependency_overrides[require_account] = (
        lambda: env["student"]
    )
    return club_id


def resubmit(env, club_id, data=None, key=None):
    return env["client"].post(
        f"/clubs/{club_id}/resubmit",
        json=data if data is not None else payload(env),
        headers={"Idempotency-Key": str(key or uuid.uuid4())},
    )


def test_student_resubmits_corrected_club(env):
    club_id = club_needing_changes(env)
    data = payload(env)
    data["description"] = "Updated engineering activities"

    response = resubmit(env, club_id, data)

    assert response.status_code == 200, response.text

    club = env["db"].get(Club, uuid.UUID(club_id))

    assert club.status == ClubStatus.PENDING
    assert club.description == data["description"]

    reviews = env["db"].scalars(
        select(ClubReview).where(
            ClubReview.club_id == club.id
        )
    ).all()

    assert len(reviews) == 1


def test_cannot_resubmit_pending_club(env):
    response = submit(env)
    assert response.status_code == 201

    assert resubmit(
        env, response.json()["id"]
    ).status_code == 409


def test_admin_cannot_resubmit_club(env):
    club_id = club_needing_changes(env)

    app.dependency_overrides[require_account] = (
        lambda: env["admin"]
    )

    assert resubmit(env, club_id).status_code == 403


def test_resubmission_is_idempotent(env):
    club_id = club_needing_changes(env)
    key = uuid.uuid4()

    first = resubmit(env, club_id, key=key)
    second = resubmit(env, club_id, key=key)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()

    count = env["db"].scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.entity_id == club_id,
            AuditLog.action == "CLUB_RESUBMITTED",
        )
    )
    assert count == 1


def test_resubmission_key_conflict(env):
    club_id = club_needing_changes(env)
    key = uuid.uuid4()

    assert resubmit(env, club_id, key=key).status_code == 200

    changed = payload(env)
    changed["name"] = "Different Club"

    assert resubmit(
        env, club_id, changed, key
    ).status_code == 409


def test_resubmission_cannot_select_student_as_teacher(env):
    club_id = club_needing_changes(env)
    data = payload(env)
    data["teacher_account_id"] = str(env["student"].id)

    assert resubmit(env, club_id, data).status_code == 404

    assert env["db"].get(
        Club, uuid.UUID(club_id)
    ).status == ClubStatus.CHANGES_REQUESTED
