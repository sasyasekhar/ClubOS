
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from queue import Queue
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.config.database import SessionLocal, engine, get_db
from app.main import app
from app.routes.auth import require_account

from app.models.account import Account
from app.models.school import School
from app.models.academic_year import AcademicYear
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.activity import Activity
from app.models.activity_revision import ActivityRevision
from app.models.activity_review import ActivityReview
from app.models.audit_log import AuditLog

from app.models.enums import (
    AccountType,
    AcademicYearStatus,
    ClubStatus,
    ClubRole,
    MembershipStatus,
    ActivityRevisionStatus,
)


@dataclass(frozen=True)
class Scenario:
    school_id: UUID
    club_id: UUID
    activity_id: UUID
    revision_id: UUID
    student_id: UUID
    teacher_id: UUID


@pytest.fixture
def scenario():
    if (
        engine.url.get_backend_name() != "postgresql"
        or engine.url.database != "clubos_test"
    ):
        pytest.fail("Requires clubos_test PostgreSQL database")

    suffix = uuid4().hex[:12]
    today = date.today()

    # Commit setup data so independent connections can see it.
    with SessionLocal.begin() as db:
        school = School(
            name=f"Review Concurrency School {suffix}",
            school_code=f"CLB-{suffix.upper()}",
        )
        db.add(school)
        db.flush()

        student = Account(
            school_id=school.id,
            account_type=AccountType.STUDENT,
            full_name="Test Student",
            username=f"student_{suffix}",
            email=f"student_{suffix}@example.com",
            password_hash="test-only",
        )

        teacher = Account(
            school_id=school.id,
            account_type=AccountType.TEACHER,
            full_name="Test Teacher",
            username=f"teacher_{suffix}",
            email=f"teacher_{suffix}@example.com",
            password_hash="test-only",
        )

        year = AcademicYear(
            school_id=school.id,
            name=f"Test Year {suffix}",
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=30),
            status=AcademicYearStatus.ACTIVE,
        )

        db.add_all([student, teacher, year])
        db.flush()

        club = Club(
            school_id=school.id,
            academic_year_id=year.id,
            created_by=student.id,
            name="Concurrency Club",
            category="STEM",
            status=ClubStatus.ACTIVE,
        )
        db.add(club)
        db.flush()

        db.add(ClubMembership(
            club_id=club.id,
            account_id=teacher.id,
            role=ClubRole.TEACHER_REP,
            status=MembershipStatus.ACTIVE,
            start_date=today,
        ))

        activity = Activity(
            club_id=club.id,
            created_by=student.id,
        )
        db.add(activity)
        db.flush()

        revision = ActivityRevision(
            activity_id=activity.id,
            revision_number=1,
            title="Robotics Workshop",
            activity_date=today,
            description="Test activity",
            category="STEM",
            status=ActivityRevisionStatus.PENDING,
            is_published=False,
            submitted_at=datetime.now(timezone.utc),
        )
        db.add(revision)
        db.flush()

        result = Scenario(
            school_id=school.id,
            club_id=club.id,
            activity_id=activity.id,
            revision_id=revision.id,
            student_id=student.id,
            teacher_id=teacher.id,
        )

    try:
        yield result
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(AuditLog).where(
                AuditLog.actor_account_id.in_([
                    result.student_id,
                    result.teacher_id,
                ])
            ))
            db.execute(delete(ActivityReview).where(
                ActivityReview.revision_id == result.revision_id
            ))
            db.execute(delete(ActivityRevision).where(
                ActivityRevision.activity_id == result.activity_id
            ))
            db.execute(delete(Activity).where(
                Activity.id == result.activity_id
            ))
            db.execute(delete(ClubMembership).where(
                ClubMembership.club_id == result.club_id
            ))
            db.execute(delete(Club).where(
                Club.id == result.club_id
            ))
            db.execute(delete(AcademicYear).where(
                AcademicYear.school_id == result.school_id
            ))
            db.execute(delete(Account).where(
                Account.school_id == result.school_id
            ))
            db.execute(delete(School).where(
                School.id == result.school_id
            ))


def concurrent_reviews(monkeypatch, scenario, requests):
    barrier = Barrier(2, timeout=10)
    backend_pids = Queue()

    def teacher_for_request(db: Session = Depends(get_db)):
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
        account = db.get(Account, scenario.teacher_id)

        backend_pids.put(
            db.scalar(select(func.pg_backend_pid()))
        )

        barrier.wait()
        return account

    monkeypatch.setitem(
        app.dependency_overrides,
        require_account,
        teacher_for_request,
    )

    path = (
        f"/clubs/{scenario.club_id}/activities/"
        f"{scenario.activity_id}/revisions/"
        f"{scenario.revision_id}/review"
    )

    def post(decision, key):
        with TestClient(app) as client:
            return client.post(
                path,
                json={
                    "decision": decision,
                    "comments": "Reviewed",
                },
                headers={"Idempotency-Key": str(key)},
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(post, decision, key)
            for decision, key in requests
        ]
        responses = [f.result(timeout=25) for f in futures]

    assert backend_pids.get_nowait() != backend_pids.get_nowait()
    return responses


def assert_single_review(scenario, expected_status):
    with SessionLocal() as db:
        revision = db.get(
            ActivityRevision, scenario.revision_id
        )
        assert revision.status == expected_status

        reviews = db.scalars(
            select(ActivityReview).where(
                ActivityReview.revision_id == scenario.revision_id
            )
        ).all()
        assert len(reviews) == 1

        audits = db.scalars(
            select(AuditLog).where(
                AuditLog.action == "ACTIVITY_REVIEWED",
                AuditLog.entity_id == str(scenario.revision_id),
            )
        ).all()
        assert len(audits) == 1
        assert audits[0].actor_account_id == scenario.teacher_id


def test_simultaneous_retry_same_key(scenario, monkeypatch):
    key = uuid4()

    responses = concurrent_reviews(
        monkeypatch,
        scenario,
        [("APPROVED", key), ("APPROVED", key)],
    )

    assert [r.status_code for r in responses] == [201, 201], [
        r.text for r in responses
    ]
    assert responses[0].json() == responses[1].json()

    assert_single_review(
        scenario, ActivityRevisionStatus.APPROVED
    )


def test_simultaneous_different_keys(scenario, monkeypatch):
    responses = concurrent_reviews(
        monkeypatch,
        scenario,
        [("APPROVED", uuid4()), ("APPROVED", uuid4())],
    )

    assert sorted(r.status_code for r in responses) == [201, 409], [
        r.text for r in responses
    ]

    assert_single_review(
        scenario, ActivityRevisionStatus.APPROVED
    )


def test_simultaneous_conflicting_decisions(scenario, monkeypatch):
    responses = concurrent_reviews(
        monkeypatch,
        scenario,
        [("APPROVED", uuid4()), ("REJECTED", uuid4())],
    )

    assert sorted(r.status_code for r in responses) == [201, 409], [
        r.text for r in responses
    ]

    winner = next(
        r for r in responses if r.status_code == 201
    ).json()

    expected = ActivityRevisionStatus(winner["status"])
    assert_single_review(scenario, expected)


@pytest.fixture
def pending_published_replacement(scenario):
    # Prepare a published v1 and a pending v2.
    with SessionLocal.begin() as db:
        original = db.get(
            ActivityRevision, scenario.revision_id
        )
        original.status = ActivityRevisionStatus.APPROVED
        original.is_published = True

        replacement = ActivityRevision(
            activity_id=scenario.activity_id,
            revision_number=2,
            title="Updated Robotics Workshop",
            activity_date=date.today(),
            description="Updated workshop details",
            category="STEM",
            status=ActivityRevisionStatus.PENDING,
            is_published=False,
            submitted_at=datetime.now(timezone.utc),
        )
        db.add(replacement)
        db.flush()
        replacement_id = replacement.id

    replacement_scenario = replace(
        scenario,
        revision_id=replacement_id,
    )

    try:
        yield scenario.revision_id, replacement_scenario
    finally:
        # Remove v2 reviews before the existing scenario
        # fixture deletes the activity and revisions.
        with SessionLocal.begin() as db:
            db.execute(
                delete(ActivityReview).where(
                    ActivityReview.revision_id == replacement_id
                )
            )


def assert_publication_state(
    activity_id,
    original_id,
    replacement_id,
    expected_published_id,
):
    with SessionLocal() as db:
        original = db.get(ActivityRevision, original_id)
        replacement = db.get(ActivityRevision, replacement_id)

        assert original is not None
        assert replacement is not None

        published = db.scalars(
            select(ActivityRevision).where(
                ActivityRevision.activity_id == activity_id,
                ActivityRevision.is_published.is_(True),
            )
        ).all()

        assert len(published) == 1
        assert published[0].id == expected_published_id


def test_concurrent_replacement_approval_same_key(
    pending_published_replacement,
    monkeypatch,
):
    original_id, scenario = pending_published_replacement
    key = uuid4()

    responses = concurrent_reviews(
        monkeypatch,
        scenario,
        [("APPROVED", key), ("APPROVED", key)],
    )

    assert [r.status_code for r in responses] == [201, 201], [
        r.text for r in responses
    ]
    assert responses[0].json() == responses[1].json()

    assert_single_review(
        scenario, ActivityRevisionStatus.APPROVED
    )

    assert_publication_state(
        scenario.activity_id,
        original_id,
        scenario.revision_id,
        scenario.revision_id,
    )


def test_concurrent_conflicting_replacement_reviews(
    pending_published_replacement,
    monkeypatch,
):
    original_id, scenario = pending_published_replacement

    responses = concurrent_reviews(
        monkeypatch,
        scenario,
        [
            ("APPROVED", uuid4()),
            ("REJECTED", uuid4()),
        ],
    )

    assert sorted(r.status_code for r in responses) == [
        201, 409
    ], [r.text for r in responses]

    winner = next(
        r for r in responses if r.status_code == 201
    ).json()

    expected_status = ActivityRevisionStatus(winner["status"])

    assert_single_review(scenario, expected_status)

    expected_published_id = (
        scenario.revision_id
        if expected_status == ActivityRevisionStatus.APPROVED
        else original_id
    )

    assert_publication_state(
        scenario.activity_id,
        original_id,
        scenario.revision_id,
        expected_published_id,
    )
