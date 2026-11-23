from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, select
from sqlalchemy.orm import Session

from app.config.database import engine, get_db
from app.main import app
from app.models.academic_year import AcademicYear
from app.models.account import Account
from app.models.activity import Activity
from app.models.activity_attachment import ActivityAttachment
from app.models.activity_review import ActivityReview
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
from app.models.auth_session import AuthSession
from app.models.club import Club
from app.models.enums import (
    AcademicYearStatus,
    AccountType,
    ActivityReviewDecision,
    ActivityRevisionStatus,
    ClubStatus,
    ReviewMode,
)
from app.models.school import School
from app.models.school_admin import SchoolAdmin
from app.routes.auth import COOKIE_NAME
from app.services.sessions import IDLE_TIMEOUT, token_hash


YEAR_START = date(2026, 6, 1)
YEAR_END = date(2027, 5, 31)


@dataclass(frozen=True)
class Scenario:
    db: Session
    client: TestClient
    years: dict[str, AcademicYear]
    account_ids: dict[str, UUID]
    headers: dict[str, dict[str, str]]

    def readiness(self, *, year="active", actor="admin", params=None):
        year_id = self.years[year].id if year in self.years else year
        self.db.commit()
        return self.client.get(
            f"/academic-years/{year_id}/closure-readiness",
            headers=self.headers.get(actor),
            params=params,
        )

    def add_club(self, *, year="active", status=ClubStatus.ACTIVE):
        academic_year = self.years[year]
        student = "other_student" if year == "other" else "student"
        club = Club(
            school_id=academic_year.school_id,
            academic_year_id=academic_year.id,
            created_by=self.account_ids[student],
            name="Robotics",
            category="STEM",
            status=status,
        )
        self.db.add(club)
        self.db.flush()
        return club

    def add_revisions(self, club, *statuses):
        activity = Activity(club_id=club.id, created_by=club.created_by)
        self.db.add(activity)
        self.db.flush()
        revisions = [
            ActivityRevision(activity_id=activity.id, revision_number=number, status=status)
            for number, status in enumerate(statuses, start=1)
        ]
        self.db.add_all(revisions)
        self.db.flush()
        return revisions


@pytest.fixture
def scenario(monkeypatch):
    if engine.url.get_backend_name() != "postgresql" or engine.url.database != "clubos_test":
        pytest.fail("Use the clubos_test PostgreSQL database")

    with engine.connect() as connection, connection.begin() as outer:
        with Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as db:
            suffix = uuid4().hex[:12]
            school = School(name="Readiness School", school_code=f"CLB-{suffix}")
            other = School(name="Other School", school_code=f"CLB-{suffix}X")
            db.add_all([school, other])
            db.flush()

            accounts = {
                name: Account(
                    school_id=school_id,
                    account_type=role,
                    full_name=name,
                    username=f"{name}_{suffix}",
                    email=f"{name}_{suffix}@example.com",
                    password_hash="test-only",
                )
                for name, role, school_id in (
                    ("admin", AccountType.SCHOOL_ADMIN, school.id),
                    ("other_admin", AccountType.SCHOOL_ADMIN, other.id),
                    ("unassigned", AccountType.SCHOOL_ADMIN, school.id),
                    ("teacher", AccountType.TEACHER, school.id),
                    ("student", AccountType.STUDENT, school.id),
                    ("other_student", AccountType.STUDENT, other.id),
                    ("owner", AccountType.PLATFORM_OWNER, None),
                )
            }
            years = {
                name: AcademicYear(
                    school_id=school_id, name=name, start_date=YEAR_START, end_date=YEAR_END, status=status,
                )
                for name, school_id, status in (
                    ("active", school.id, AcademicYearStatus.ACTIVE),
                    ("closed", school.id, AcademicYearStatus.CLOSED),
                    ("archived", school.id, AcademicYearStatus.ARCHIVED),
                    ("other", other.id, AcademicYearStatus.ACTIVE),
                )
            }
            db.add_all([*accounts.values(), *years.values()])
            db.flush()
            db.add_all([
                SchoolAdmin(school_id=school.id, account_id=accounts["admin"].id),
                SchoolAdmin(school_id=other.id, account_id=accounts["other_admin"].id),
            ])
            headers = {}
            expires_at = datetime.now(timezone.utc) + IDLE_TIMEOUT
            for name, account in accounts.items():
                cookie, tab = uuid4().hex, uuid4().hex
                db.add(AuthSession(account_id=account.id, token_hash=token_hash(cookie, tab), expires_at=expires_at))
                headers[name] = {"Cookie": f"{COOKIE_NAME}={cookie}", "X-ClubOS-Tab": tab}
            db.commit()

            def request_db():
                with Session(bind=connection, join_transaction_mode="create_savepoint", autoflush=False) as session:
                    yield session

            monkeypatch.setitem(app.dependency_overrides, get_db, request_db)
            try:
                with TestClient(app) as client:
                    yield Scenario(db, client, years, {name: account.id for name, account in accounts.items()}, headers)
            finally:
                db.close()
                outer.rollback()


def counts(**nonzero):
    return {status.value: nonzero.get(status.value, 0) for status in ActivityRevisionStatus}


def blocker(club, revision):
    return {"club_id": str(club.id), "activity_id": str(revision.activity_id), "revision_id": str(revision.id)}


def assert_readiness(response, year, expected_counts, blockers, *, can_close):
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.json() == {
        "academic_year_id": str(year.id),
        "status": year.status.value,
        "counts": expected_counts,
        "can_close": can_close,
        "pending_review_blocker_count": expected_counts["PENDING"],
        "pending_review_blockers": blockers,
    }


@pytest.mark.parametrize("year", ["active", "closed", "archived"])
def test_readiness_of_an_empty_year(scenario, year):
    assert_readiness(scenario.readiness(year=year), scenario.years[year], counts(), [], can_close=year == "active")


def test_resolved_revisions_count_once_with_multiple_review_rows(scenario):
    club = scenario.add_club()
    approved, rejected = scenario.add_revisions(club, ActivityRevisionStatus.APPROVED, ActivityRevisionStatus.REJECTED)
    scenario.db.add_all([
        ActivityReview(
            revision_id=revision.id,
            actor_account_id=scenario.account_ids["teacher"],
            review_mode=ReviewMode.DIRECT,
            decision=decision,
            comments="An explanation for the review outcome.",
        )
        for revision, decision in (
            (approved, ActivityReviewDecision.APPROVED),
            (approved, ActivityReviewDecision.APPROVED),
            (rejected, ActivityReviewDecision.REJECTED),
        )
    ])
    assert_readiness(
        scenario.readiness(), scenario.years["active"], counts(APPROVED=1, REJECTED=1), [], can_close=True,
    )


@pytest.mark.parametrize("club_status", [ClubStatus.ACTIVE, ClubStatus.ARCHIVED])
def test_pending_review_blocks_even_when_the_club_is_inactive(scenario, club_status):
    club = scenario.add_club(status=club_status)
    pending, = scenario.add_revisions(club, ActivityRevisionStatus.PENDING)
    assert_readiness(
        scenario.readiness(), scenario.years["active"], counts(PENDING=1), [blocker(club, pending)], can_close=False,
    )


def test_mixed_revisions_across_clubs_keep_their_own_status_counts(scenario):
    first = scenario.add_club()
    pending, _, _ = scenario.add_revisions(
        first, ActivityRevisionStatus.PENDING, ActivityRevisionStatus.DRAFT, ActivityRevisionStatus.CHANGES_REQUESTED,
    )
    second = scenario.add_club()
    another_pending, _ = scenario.add_revisions(second, ActivityRevisionStatus.PENDING, ActivityRevisionStatus.DRAFT)
    blockers = sorted([blocker(first, pending), blocker(second, another_pending)], key=lambda row: row["revision_id"])
    assert_readiness(
        scenario.readiness(), scenario.years["active"], counts(PENDING=2, DRAFT=2, CHANGES_REQUESTED=1), blockers,
        can_close=False,
    )


def test_unfinished_revisions_do_not_block_closure_on_their_own(scenario):
    club = scenario.add_club()
    scenario.add_revisions(club, ActivityRevisionStatus.DRAFT, ActivityRevisionStatus.CHANGES_REQUESTED)
    assert_readiness(
        scenario.readiness(), scenario.years["active"], counts(DRAFT=1, CHANGES_REQUESTED=1), [], can_close=True,
    )


def test_readiness_uses_only_the_requested_year_in_the_admins_school(scenario):
    own = scenario.add_club()
    scenario.add_revisions(own, ActivityRevisionStatus.APPROVED)
    for year in ("closed", "other"):
        unrelated = scenario.add_club(year=year)
        scenario.add_revisions(unrelated, ActivityRevisionStatus.PENDING)

    school_override = {"school_id": str(scenario.years["other"].school_id)}
    assert_readiness(
        scenario.readiness(params=school_override), scenario.years["active"], counts(APPROVED=1), [], can_close=True,
    )
    foreign = scenario.readiness(year="other", params=school_override)
    missing = scenario.readiness(year=uuid4())
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert foreign.headers["Cache-Control"] == "private, no-store"
    assert scenario.readiness(year="active", actor="other_admin").status_code == 404


@pytest.mark.parametrize(
    "actor,expected_status", [(None, 401), ("student", 403), ("teacher", 403), ("owner", 403), ("unassigned", 403)],
)
def test_readiness_requires_an_authenticated_assigned_school_admin(scenario, actor, expected_status):
    assert scenario.readiness(actor=actor).status_code == expected_status


def test_readiness_leaves_records_and_audit_history_intact(scenario):
    db = scenario.db
    club = scenario.add_club()
    approved, pending = scenario.add_revisions(club, ActivityRevisionStatus.APPROVED, ActivityRevisionStatus.PENDING)
    db.add_all([
        ActivityReview(
            revision_id=approved.id, actor_account_id=scenario.account_ids["teacher"], review_mode=ReviewMode.DIRECT,
            decision=ActivityReviewDecision.APPROVED,
        ),
        ActivityAttachment(
            revision_id=pending.id, file_name="agenda.pdf", storage_key="private/agenda", file_type="application/pdf",
        ),
        AuditLog(
            actor_account_id=scenario.account_ids["student"], action="ACTIVITY_SUBMITTED",
            entity_type="activity_revision", entity_id=str(pending.id),
        ),
    ])
    db.commit()

    def snapshot():
        statements = (
            select(AcademicYear.__table__).where(AcademicYear.id == scenario.years["active"].id),
            select(Club.__table__).where(Club.id == club.id),
            select(Activity.__table__).where(Activity.club_id == club.id).order_by(Activity.id),
            select(ActivityRevision.__table__).where(ActivityRevision.activity_id == pending.activity_id)
            .order_by(ActivityRevision.id),
            select(ActivityReview.__table__).where(ActivityReview.revision_id.in_([approved.id, pending.id]))
            .order_by(ActivityReview.id),
            select(ActivityAttachment.__table__).where(ActivityAttachment.revision_id.in_([approved.id, pending.id]))
            .order_by(ActivityAttachment.id),
            select(AuditLog.__table__).where(AuditLog.actor_account_id.in_(scenario.account_ids.values()))
            .order_by(AuditLog.id),
        )
        return [db.execute(statement).all() for statement in statements]

    before = snapshot()
    for _ in range(2):
        assert_readiness(
            scenario.readiness(), scenario.years["active"], counts(APPROVED=1, PENDING=1), [blocker(club, pending)],
            can_close=False,
        )
    assert snapshot() == before


def test_large_year_uses_one_data_query_and_a_bounded_blocker_list(scenario):
    db = scenario.db
    clubs = [scenario.add_club() for _ in range(6)]
    activities = [
        Activity(id=uuid4(), club_id=club.id, created_by=club.created_by)
        for club in clubs for _ in range(12)
    ]
    db.add_all(activities)
    db.flush()
    status_sequence = tuple(ActivityRevisionStatus) * 3
    revisions = [
        {"id": uuid4(), "activity_id": activity.id, "revision_number": number, "status": status}
        for activity in activities
        for number, status in enumerate(status_sequence, start=1)
    ]
    db.execute(insert(ActivityRevision), revisions)
    db.commit()

    club_ids_by_activity = {activity.id: activity.club_id for activity in activities}
    pending_revisions = sorted(
        (row for row in revisions if row["status"] == ActivityRevisionStatus.PENDING), key=lambda row: row["id"],
    )
    expected_blockers = [
        {
            "club_id": str(club_ids_by_activity[row["activity_id"]]),
            "activity_id": str(row["activity_id"]),
            "revision_id": str(row["id"]),
        }
        for row in pending_revisions[:50]
    ]

    connection = db.get_bind()
    data_queries = []

    def track_readiness_query(_conn, cursor, statement, _params, _context, _executemany):
        if "activity_revisions" in statement.lower():
            data_queries.append((statement, cursor.rowcount))

    event.listen(connection, "after_cursor_execute", track_readiness_query)
    try:
        response = scenario.readiness()
    finally:
        event.remove(connection, "after_cursor_execute", track_readiness_query)

    assert_readiness(
        response, scenario.years["active"], dict.fromkeys(counts(), len(pending_revisions)), expected_blockers,
        can_close=False,
    )
    (query, returned_rows), = data_queries
    assert "LIMIT" in query.upper()
    assert 0 < returned_rows <= 50


def test_database_failure_returns_a_private_retriable_error(scenario, caplog):
    connection = scenario.db.get_bind()
    private_detail = "readiness-private-db-detail"

    def fail_readiness_query(_conn, cursor, statement, _params, _context, _executemany):
        if "activity_revisions" in statement.lower():
            cursor.execute("SELECT %s::integer", (private_detail,))

    event.listen(connection, "before_cursor_execute", fail_readiness_query)
    try:
        response = scenario.readiness()
    finally:
        event.remove(connection, "before_cursor_execute", fail_readiness_query)

    assert response.status_code == 503
    assert response.json() == {"detail": "Closure readiness is temporarily unavailable."}
    assert response.headers["Cache-Control"] == "private, no-store"
    assert private_detail not in caplog.text
    assert_readiness(scenario.readiness(), scenario.years["active"], counts(), [], can_close=True)
