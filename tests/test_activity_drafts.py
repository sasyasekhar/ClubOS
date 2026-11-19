
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

from app.models.activity_review import ActivityReview

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


def revision_url(env, created):
    return (
        f"/clubs/{env['club'].id}/activities/"
        f"{created['activity_id']}/revisions/"
        f"{created['revision_id']}"
    )


def test_student_edits_draft(env):
    created = create(env).json()
    data = draft_data()
    data["title"] = "Advanced Robotics Workshop"

    response = env["client"].put(
        revision_url(env, created),
        json=data,
    )

    assert response.status_code == 200, response.text

    revision = env["db"].get(
        ActivityRevision, created["revision_id"]
    )
    assert revision.title == data["title"]
    assert revision.status == ActivityRevisionStatus.DRAFT


def test_identical_edit_does_not_duplicate_audit(env):
    created = create(env).json()
    data = draft_data()
    data["title"] = "Updated Workshop"

    url = revision_url(env, created)

    assert env["client"].put(url, json=data).status_code == 200
    assert env["client"].put(url, json=data).status_code == 200

    count = env["db"].scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.entity_id == created["revision_id"],
            AuditLog.action == "ACTIVITY_DRAFT_UPDATED",
        )
    )
    assert count == 1


def test_student_submits_draft(env):
    created = create(env).json()

    response = env["client"].post(
        revision_url(env, created) + "/submit"
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "PENDING"

    revision = env["db"].get(
        ActivityRevision, created["revision_id"]
    )

    assert revision.submitted_at is not None
    assert revision.is_published is False


def test_duplicate_submission_creates_one_audit(env):
    created = create(env).json()
    url = revision_url(env, created) + "/submit"

    first = env["client"].post(url)
    second = env["client"].post(url)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()

    count = env["db"].scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.entity_id == created["revision_id"],
            AuditLog.action == "ACTIVITY_SUBMITTED",
        )
    )
    assert count == 1


def test_submitted_draft_cannot_be_edited(env):
    created = create(env).json()
    url = revision_url(env, created)

    assert env["client"].post(url + "/submit").status_code == 200

    response = env["client"].put(url, json=draft_data())
    assert response.status_code == 409


def test_other_student_cannot_edit_or_submit(env):
    created = create(env).json()

    app.dependency_overrides[require_account] = (
        lambda: env["other"]
    )

    url = revision_url(env, created)

    assert env["client"].put(
        url, json=draft_data()
    ).status_code == 403

    assert env["client"].post(
        url + "/submit"
    ).status_code == 403


def test_closed_year_blocks_submission(env):
    created = create(env).json()

    env["year"].status = AcademicYearStatus.CLOSED
    env["db"].commit()

    response = env["client"].post(
        revision_url(env, created) + "/submit"
    )

    assert response.status_code == 409


def submitted_for_review(env):
    created_response = create(env)
    assert created_response.status_code == 201
    created = created_response.json()

    response = env["client"].post(
        revision_url(env, created) + "/submit"
    )
    assert response.status_code == 200, response.text

    env["db"].add(ClubMembership(
        club_id=env["club"].id,
        account_id=env["teacher"].id,
        role=ClubRole.TEACHER_REP,
        status=MembershipStatus.ACTIVE,
        start_date=date.today(),
    ))
    env["db"].commit()

    app.dependency_overrides[require_account] = (
        lambda: env["teacher"]
    )

    return created


def review_request(env, created, decision, comments=None, key=None):
    return env["client"].post(
        revision_url(env, created) + "/review",
        headers={"Idempotency-Key": str(key or uuid4())},
        json={
            "decision": decision,
            "comments": comments,
        },
    )


@pytest.mark.parametrize(
    "decision,expected",
    [
        ("APPROVED", ActivityRevisionStatus.APPROVED),
        ("REJECTED", ActivityRevisionStatus.REJECTED),
        ("CHANGES_REQUESTED", ActivityRevisionStatus.CHANGES_REQUESTED),
    ],
)
def test_teacher_review_decisions(env, decision, expected):
    created = submitted_for_review(env)

    response = review_request(
        env, created, decision, "Reviewed activity."
    )

    assert response.status_code == 201, response.text

    revision = env["db"].get(
        ActivityRevision, created["revision_id"]
    )

    assert revision.status == expected
    assert revision.is_published is (
    decision == "APPROVED"
        )

    review = env["db"].get(
        ActivityReview, response.json()["review_id"]
    )
    assert review.actor_account_id == env["teacher"].id
    assert review.represented_account_id is None


@pytest.mark.parametrize(
    "decision",
    ["REJECTED", "CHANGES_REQUESTED"],
)
def test_review_explanation_required(env, decision):
    created = submitted_for_review(env)

    response = review_request(env, created, decision, "   ")

    assert response.status_code == 422


def test_review_retry_is_idempotent(env):
    created = submitted_for_review(env)
    key = uuid4()

    first = review_request(env, created, "APPROVED", key=key)
    second = review_request(env, created, "APPROVED", key=key)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()

    count = env["db"].scalar(
        select(func.count()).select_from(ActivityReview).where(
            ActivityReview.revision_id == created["revision_id"]
        )
    )
    assert count == 1


def test_second_review_rejected(env):
    created = submitted_for_review(env)

    assert review_request(
        env, created, "APPROVED"
    ).status_code == 201

    assert review_request(
        env, created, "REJECTED", "Different review"
    ).status_code == 409


def test_student_cannot_review(env):
    created = submitted_for_review(env)

    app.dependency_overrides[require_account] = (
        lambda: env["student"]
    )

    assert review_request(
        env, created, "APPROVED"
    ).status_code == 403


def test_unassigned_teacher_cannot_review(env):
    created = submitted_for_review(env)

    env["db"].query(ClubMembership).filter(
        ClubMembership.club_id == env["club"].id,
        ClubMembership.role == ClubRole.TEACHER_REP,
    ).delete()
    env["db"].commit()

    assert review_request(
        env, created, "APPROVED"
    ).status_code == 403


def test_cross_school_teacher_cannot_review(env):
    created = submitted_for_review(env)

    suffix = uuid4().hex[:12]
    other_school = School(
        name=f"Outside School {suffix}",
        school_code=f"CLB-{suffix.upper()}",
    )
    env["db"].add(other_school)
    env["db"].flush()

    outsider = Account(
        school_id=other_school.id,
        account_type=AccountType.TEACHER,
        full_name="Outside Teacher",
        username=f"outside_{suffix}",
        email=f"outside_{suffix}@example.com",
        password_hash="test-only",
    )
    env["db"].add(outsider)
    env["db"].commit()

    app.dependency_overrides[require_account] = (
        lambda: outsider
    )

    assert review_request(
        env, created, "APPROVED"
    ).status_code == 404


def test_draft_cannot_be_reviewed(env):
    response = create(env)
    assert response.status_code == 201
    created = response.json()

    env["db"].add(ClubMembership(
        club_id=env["club"].id,
        account_id=env["teacher"].id,
        role=ClubRole.TEACHER_REP,
        status=MembershipStatus.ACTIVE,
        start_date=date.today(),
    ))
    env["db"].commit()

    app.dependency_overrides[require_account] = (
        lambda: env["teacher"]
    )

    assert review_request(
        env, created, "APPROVED"
    ).status_code == 409

def activity_needing_corrections(env):
    created = submitted_for_review(env)

    response = review_request(
        env,
        created,
        "CHANGES_REQUESTED",
        "Please improve the activity description.",
    )
    assert response.status_code == 201, response.text

    app.dependency_overrides[require_account] = (
        lambda: env["student"]
    )

    return created


def correct_activity(env, created, data=None, key=None):
    return env["client"].post(
        revision_url(env, created) + "/correct",
        json=data if data is not None else draft_data(),
        headers={
            "Idempotency-Key": str(key or uuid4())
        },
    )


def test_corrected_revision_preserves_review_history(env):
    original = activity_needing_corrections(env)

    data = draft_data()
    data["description"] = "Updated workshop details"

    response = correct_activity(env, original, data)

    assert response.status_code == 201, response.text

    corrected = response.json()

    assert corrected["revision_number"] == 2
    assert corrected["revision_id"] != original["revision_id"]
    assert corrected["activity_id"] == original["activity_id"]
    assert corrected["status"] == "DRAFT"

    old_revision = env["db"].get(
        ActivityRevision, original["revision_id"]
    )
    new_revision = env["db"].get(
        ActivityRevision, corrected["revision_id"]
    )

    assert old_revision.status == (
        ActivityRevisionStatus.CHANGES_REQUESTED
    )
    assert old_revision.description == (
        "Introduction to robotics"
    )

    assert new_revision.description == data["description"]
    assert new_revision.is_published is False
    assert new_revision.submitted_at is None

    reviews = env["db"].scalars(
        select(ActivityReview).where(
            ActivityReview.revision_id == old_revision.id
        )
    ).all()

    assert len(reviews) == 1

    submitted = env["client"].post(
        revision_url(env, corrected) + "/submit"
    )

    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "PENDING"


def test_correction_retry_is_idempotent(env):
    original = activity_needing_corrections(env)
    key = uuid4()

    first = correct_activity(env, original, key=key)
    second = correct_activity(env, original, key=key)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()

    count = env["db"].scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.action == "ACTIVITY_CORRECTION_STARTED",
            AuditLog.entity_id == first.json()["revision_id"],
        )
    )

    assert count == 1


def test_correction_key_conflict(env):
    original = activity_needing_corrections(env)
    key = uuid4()

    assert correct_activity(
        env, original, key=key
    ).status_code == 201

    changed = draft_data()
    changed["title"] = "Different Workshop"

    assert correct_activity(
        env, original, changed, key
    ).status_code == 409


def test_cannot_create_two_corrections(env):
    original = activity_needing_corrections(env)

    assert correct_activity(
        env, original
    ).status_code == 201

    assert correct_activity(
        env, original
    ).status_code == 409


@pytest.mark.parametrize("actor", ["teacher", "other"])
def test_unauthorized_correction(env, actor):
    original = activity_needing_corrections(env)

    app.dependency_overrides[require_account] = (
        lambda: env[actor]
    )

    response = correct_activity(env, original)

    assert response.status_code == 403


def test_pending_revision_cannot_be_corrected(env):
    original = submitted_for_review(env)

    app.dependency_overrides[require_account] = (
        lambda: env["student"]
    )

    assert correct_activity(
        env, original
    ).status_code == 409


def test_approved_revision_cannot_be_corrected(env):
    original = submitted_for_review(env)

    assert review_request(
        env, original, "APPROVED"
    ).status_code == 201

    app.dependency_overrides[require_account] = (
        lambda: env["student"]
    )

    assert correct_activity(
        env, original
    ).status_code == 409


def test_approved_activity_visible_to_same_school(env):
    created = submitted_for_review(env)

    response = review_request(env, created, "APPROVED")
    assert response.status_code == 201, response.text

    # Another student in the same school can view it.
    app.dependency_overrides[require_account] = (
        lambda: env["other"]
    )

    feed = env["client"].get("/activities/published")
    assert feed.status_code == 200, feed.text

    items = feed.json()
    assert len(items) == 1
    assert items[0]["activity_id"] == created["activity_id"]
    assert items[0]["revision_id"] == created["revision_id"]
    assert items[0]["title"] == "Robotics Workshop"


def test_pending_activity_not_visible(env):
    created = submitted_for_review(env)

    app.dependency_overrides[require_account] = (
        lambda: env["other"]
    )

    response = env["client"].get("/activities/published")

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize(
    "decision",
    ["REJECTED", "CHANGES_REQUESTED"],
)
def test_unapproved_activity_not_visible(env, decision):
    created = submitted_for_review(env)

    assert review_request(
        env, created, decision, "Changes required"
    ).status_code == 201

    app.dependency_overrides[require_account] = (
        lambda: env["other"]
    )

    response = env["client"].get("/activities/published")

    assert response.status_code == 200
    assert response.json() == []


def test_other_school_cannot_view_published_activity(env):
    from uuid import uuid4

    created = submitted_for_review(env)
    assert review_request(
        env, created, "APPROVED"
    ).status_code == 201

    suffix = uuid4().hex[:12]

    outsider_school = School(
        name=f"Other School {suffix}",
        school_code=f"CLB-{suffix.upper()}",
    )
    env["db"].add(outsider_school)
    env["db"].flush()

    outsider = Account(
        school_id=outsider_school.id,
        account_type=AccountType.STUDENT,
        full_name="Outside Student",
        username=f"outside_{suffix}",
        email=f"outside_{suffix}@example.com",
        password_hash="test-only",
    )
    env["db"].add(outsider)
    env["db"].commit()

    app.dependency_overrides[require_account] = (
        lambda: outsider
    )

    response = env["client"].get("/activities/published")

    assert response.status_code == 200
    assert response.json() == []


def test_archived_club_activity_is_hidden(env):
    created = submitted_for_review(env)
    assert review_request(
        env, created, "APPROVED"
    ).status_code == 201

    env["club"].status = ClubStatus.ARCHIVED
    env["db"].commit()

    app.dependency_overrides[require_account] = (
        lambda: env["other"]
    )

    response = env["client"].get("/activities/published")

    assert response.status_code == 200
    assert response.json() == []


def test_platform_owner_denied_published_feed(env):
    from types import SimpleNamespace

    app.dependency_overrides[require_account] = (
        lambda: SimpleNamespace(
            account_type=AccountType.PLATFORM_OWNER,
            school_id=None,
            is_active=True,
        )
    )

    response = env["client"].get("/activities/published")
    assert response.status_code == 403
