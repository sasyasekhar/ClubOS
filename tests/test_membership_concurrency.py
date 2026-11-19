from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
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
from app.models.account import Account
from app.models.academic_year import AcademicYear
from app.models.audit_log import AuditLog
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.enums import (
    AcademicYearStatus,
    AccountType,
    ClubRole,
    ClubStatus,
    MembershipStatus,
)
from app.models.school import School
from app.models.school_admin import SchoolAdmin
from app.routes.auth import require_account


@dataclass(frozen=True)
class MembershipScenario:
    club_id: UUID
    admin_id: UUID
    student_id: UUID
    teacher_id: UUID
    student_membership_id: UUID
    teacher_membership_id: UUID


@pytest.fixture
def memberships():
    if engine.url.get_backend_name() != "postgresql" or engine.url.database != "clubos_test":
        pytest.fail("Use the clubos_test PostgreSQL database")

    suffix = uuid4().hex[:16]
    today = date.today()
    with SessionLocal.begin() as db:
        school = School(name="Membership Concurrency School", school_code=f"CLB-{suffix}")
        db.add(school)
        db.flush()
        school_id = school.id

        admin, student, teacher = (
            Account(
                school_id=school_id,
                account_type=role,
                full_name=role.value,
                username=f"{role.value.lower()}_{suffix}",
                email=f"{role.value.lower()}_{suffix}@example.com",
                password_hash="unused",
            )
            for role in (AccountType.SCHOOL_ADMIN, AccountType.STUDENT, AccountType.TEACHER)
        )
        year = AcademicYear(
            school_id=school_id,
            name="Membership Concurrency Year",
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=30),
            status=AcademicYearStatus.ACTIVE,
        )
        db.add_all([admin, student, teacher, year])
        db.flush()

        club = Club(
            school_id=school_id,
            academic_year_id=year.id,
            created_by=student.id,
            name="Membership Concurrency Club",
            category="STEM",
            status=ClubStatus.ACTIVE,
        )
        db.add_all([club, SchoolAdmin(school_id=school_id, account_id=admin.id)])
        db.flush()

        student_membership = ClubMembership(
            club_id=club.id,
            account_id=student.id,
            role=ClubRole.STUDENT_REP,
            status=MembershipStatus.ACTIVE,
            start_date=today,
        )
        teacher_membership = ClubMembership(
            club_id=club.id,
            account_id=teacher.id,
            role=ClubRole.TEACHER_REP,
            status=MembershipStatus.PENDING,
            start_date=today,
        )
        db.add_all([student_membership, teacher_membership])
        db.flush()
        scenario = MembershipScenario(
            club_id=club.id,
            admin_id=admin.id,
            student_id=student.id,
            teacher_id=teacher.id,
            student_membership_id=student_membership.id,
            teacher_membership_id=teacher_membership.id,
        )

    try:
        yield scenario
    finally:
        with SessionLocal.begin() as db:
            account_ids = select(Account.id).where(Account.school_id == school_id)
            db.execute(delete(AuditLog).where(AuditLog.actor_account_id.in_(account_ids)))
            db.execute(delete(ClubMembership).where(ClubMembership.club_id == scenario.club_id))
            db.execute(delete(Club).where(Club.id == scenario.club_id))
            db.execute(delete(SchoolAdmin).where(SchoolAdmin.school_id == school_id))
            db.execute(delete(AcademicYear).where(AcademicYear.school_id == school_id))
            db.execute(delete(Account).where(Account.school_id == school_id))
            db.execute(delete(School).where(School.id == school_id))


def post_concurrently(monkeypatch, actor_id, path, *, payload=None, use_idempotency_keys=False):
    ready = Barrier(2, timeout=10)
    backend_pids = Queue()

    def account_for_request(db: Session = Depends(get_db)):
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
        account = db.get(Account, actor_id)
        assert account is not None
        backend_pids.put(db.scalar(select(func.pg_backend_pid())))
        # Both requests hold a database connection before either enters the handler.
        ready.wait()
        return account

    def post():
        headers = {"Idempotency-Key": str(uuid4())} if use_idempotency_keys else {}
        with TestClient(app) as client:
            return client.post(path, json=payload, headers=headers)

    monkeypatch.setitem(app.dependency_overrides, require_account, account_for_request)
    with ThreadPoolExecutor(max_workers=2) as executor:
        requests = [executor.submit(post) for _ in range(2)]
        responses = [request.result(timeout=25) for request in requests]

    first_pid = backend_pids.get_nowait()
    second_pid = backend_pids.get_nowait()
    assert first_pid != second_pid, "Requests must use separate PostgreSQL connections"
    return responses


def membership_audit_events(db, action, membership_id):
    return db.scalars(
        select(AuditLog).where(
            AuditLog.action == action,
            AuditLog.entity_id == str(membership_id),
        )
    ).all()


def test_concurrent_teacher_acceptance_records_one_event(memberships, monkeypatch):
    responses = post_concurrently(
        monkeypatch,
        memberships.teacher_id,
        f"/clubs/{memberships.club_id}/teacher-invitation",
        payload={"decision": "ACCEPT"},
    )
    assert [response.status_code for response in responses] == [200, 200], [
        response.text for response in responses
    ]

    with SessionLocal() as db:
        teacher_memberships = db.scalars(
            select(ClubMembership).where(
                ClubMembership.club_id == memberships.club_id,
                ClubMembership.role == ClubRole.TEACHER_REP,
            )
        ).all()
        assert len(teacher_memberships) == 1
        membership = teacher_memberships[0]
        assert membership.id == memberships.teacher_membership_id
        assert membership.account_id == memberships.teacher_id
        assert membership.status == MembershipStatus.ACTIVE

        events = membership_audit_events(db, "TEACHER_INVITATION_ACCEPTED", membership.id)
        assert len(events) == 1
        assert events[0].actor_account_id == memberships.teacher_id


def test_concurrent_rejoins_create_one_current_membership(memberships, monkeypatch):
    original_end_date = date.today()
    with SessionLocal.begin() as db:
        original = db.get(ClubMembership, memberships.student_membership_id)
        original.status = MembershipStatus.ENDED
        original.end_date = original_end_date

    responses = post_concurrently(
        monkeypatch,
        memberships.admin_id,
        f"/clubs/{memberships.club_id}/memberships/{memberships.student_membership_id}/rejoin",
        use_idempotency_keys=True,
    )
    assert sorted(response.status_code for response in responses) == [201, 409], [
        response.text for response in responses
    ]
    created = next(response for response in responses if response.status_code == 201)
    new_id = UUID(created.json()["membership_id"])
    assert new_id != memberships.student_membership_id

    with SessionLocal() as db:
        history = {
            membership.id: membership
            for membership in db.scalars(
                select(ClubMembership).where(
                    ClubMembership.club_id == memberships.club_id,
                    ClubMembership.role == ClubRole.STUDENT_REP,
                )
            )
        }
        assert set(history) == {memberships.student_membership_id, new_id}
        original = history[memberships.student_membership_id]
        assert original.status == MembershipStatus.ENDED
        assert original.end_date == original_end_date
        assert history[new_id].status == MembershipStatus.ACTIVE
        assert history[new_id].account_id == memberships.student_id


def test_concurrent_membership_ending_records_one_event(memberships, monkeypatch):
    reason = "Administrative representative change"
    responses = post_concurrently(
        monkeypatch,
        memberships.admin_id,
        f"/clubs/{memberships.club_id}/memberships/{memberships.student_membership_id}/end",
        payload={"reason": reason},
        use_idempotency_keys=True,
    )
    assert sorted(response.status_code for response in responses) == [200, 409], [
        response.text for response in responses
    ]

    with SessionLocal() as db:
        membership = db.get(ClubMembership, memberships.student_membership_id)
        assert membership is not None
        assert membership.status == MembershipStatus.ENDED
        assert membership.end_date is not None

        events = membership_audit_events(db, "REPRESENTATIVE_ENDED", membership.id)
        assert len(events) == 1
        assert events[0].actor_account_id == memberships.admin_id
        assert events[0].details["reason"] == reason
