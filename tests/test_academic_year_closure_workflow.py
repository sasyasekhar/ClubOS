from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from queue import Queue
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.orm import Session

from app.config.database import SessionLocal, engine, get_db
from app.main import app
from app.models.academic_year import AcademicYear
from app.models.academic_year_closure_request import AcademicYearClosureRequest
from app.models.account import Account
from app.models.activity import Activity
from app.models.activity_attachment import ActivityAttachment
from app.models.activity_review import ActivityReview
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
from app.models.auth_session import AuthSession
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.enums import (
    AcademicYearStatus,
    AccountType,
    ActivityReviewDecision,
    ActivityRevisionStatus,
    ClubRole,
    ClubStatus,
    MembershipStatus,
    ReviewMode,
)
from app.models.school import School
from app.models.school_admin import SchoolAdmin
from app.routes.auth import COOKIE_NAME
from app.services.academic_year_closure_workflow import (
    confirm_year_closure,
    request_year_closure,
    set_closure_teacher,
)
from app.services.academic_years import AcademicYearError
from app.services.activity_drafts import ActivityError
from app.services.activity_workflow import submit_activity_draft
from app.services.sessions import IDLE_TIMEOUT, token_hash


PATH = "/academic-years"
DESIGNATION = f"{PATH}/closure-teacher"
DESIGNATED = "CLOSURE_TEACHER_DESIGNATED"
REQUESTED = "ACADEMIC_YEAR_CLOSURE_REQUESTED"
CLOSED = "ACADEMIC_YEAR_CLOSED"
WAIT = 8
START = date(2026, 6, 1)
END = date(2027, 5, 31)


@dataclass(frozen=True)
class Scenario:
    schools: dict[str, UUID]
    accounts: dict[str, UUID]
    years: dict[str, UUID]
    credentials: dict[str, tuple[str, str]]

    def auth(self, actor):
        if actor is None:
            return {}
        cookie, tab = self.credentials[actor]
        return {"Cookie": f"{COOKIE_NAME}={cookie}", "X-ClubOS-Tab": tab}

    def designate(self, client, teacher="teacher", actor="admin"):
        return client.put(DESIGNATION, headers=self.auth(actor), json={
            "teacher_account_id": str(self.accounts.get(teacher, teacher)),
        })

    def request(self, client, *, year="active", actor="teacher", key=None):
        return client.post(f"{PATH}/{self.years[year]}/closure-request", headers={
            **self.auth(actor), "Idempotency-Key": str(key or uuid4()),
        })

    def pending(self, client, year="active", actor="admin"):
        return client.get(f"{PATH}/{self.years[year]}/closure-request", headers=self.auth(actor))

    def confirm(self, client, request_id, *, year="active", actor="admin"):
        return client.post(f"{PATH}/{self.years[year]}/confirm-closure", headers=self.auth(actor), json={
            "request_id": str(request_id),
        })


@pytest.fixture
def scenario():
    if engine.url.get_backend_name() != "postgresql" or engine.url.database != "clubos_test":
        pytest.fail("Use the clubos_test PostgreSQL database")

    suffix = uuid4().hex[:12]
    with SessionLocal.begin() as db:
        schools = {name: School(name=name, school_code=f"CLB-{name}-{suffix}") for name in ("own", "other")}
        db.add_all(schools.values())
        db.flush()
        identities = (
            ("admin", AccountType.SCHOOL_ADMIN, "own"),
            ("unassigned", AccountType.SCHOOL_ADMIN, "own"),
            ("teacher", AccountType.TEACHER, "own"),
            ("replacement", AccountType.TEACHER, "own"),
            ("inactive", AccountType.TEACHER, "own"),
            ("student", AccountType.STUDENT, "own"),
            ("other_admin", AccountType.SCHOOL_ADMIN, "other"),
            ("other_teacher", AccountType.TEACHER, "other"),
            ("owner", AccountType.PLATFORM_OWNER, None),
        )
        accounts = {
            name: Account(
                school_id=schools[school].id if school else None,
                account_type=role, full_name=name, username=f"{name}_{suffix}",
                email=f"{name}_{suffix}@example.com", password_hash="test-only", is_active=name != "inactive",
            )
            for name, role, school in identities
        }
        years = {
            name: AcademicYear(
                school_id=schools[school].id, name=name, start_date=START, end_date=END, status=year_status,
            )
            for name, school, year_status in (
                ("active", "own", AcademicYearStatus.ACTIVE),
                ("historical", "own", AcademicYearStatus.CLOSED),
                ("other", "other", AcademicYearStatus.ACTIVE),
            )
        }
        db.add_all([*accounts.values(), *years.values()])
        db.flush()
        db.add_all([
            SchoolAdmin(school_id=schools["own"].id, account_id=accounts["admin"].id),
            SchoolAdmin(school_id=schools["other"].id, account_id=accounts["other_admin"].id),
        ])
        actors = {**accounts, "admin_tab": accounts["admin"], "teacher_tab": accounts["teacher"]}
        credentials = {actor: (uuid4().hex, uuid4().hex) for actor in actors}
        db.add_all(
            AuthSession(
                account_id=account.id, token_hash=token_hash(*credentials[actor]),
                expires_at=datetime.now(timezone.utc) + IDLE_TIMEOUT,
            )
            for actor, account in actors.items()
        )
        data = Scenario(
            {name: school.id for name, school in schools.items()},
            {name: account.id for name, account in accounts.items()},
            {name: year.id for name, year in years.items()},
            credentials,
        )

    try:
        yield data
    finally:
        schools = tuple(data.schools.values())
        clubs = select(Club.id).where(Club.school_id.in_(schools))
        activities = select(Activity.id).where(Activity.club_id.in_(clubs))
        revisions = select(ActivityRevision.id).where(ActivityRevision.activity_id.in_(activities))
        with SessionLocal.begin() as db:
            db.execute(delete(AuditLog).where(AuditLog.actor_account_id.in_(data.accounts.values())))
            db.execute(delete(AcademicYearClosureRequest).where(AcademicYearClosureRequest.school_id.in_(schools)))
            db.execute(delete(ActivityAttachment).where(ActivityAttachment.revision_id.in_(revisions)))
            db.execute(delete(ActivityReview).where(ActivityReview.revision_id.in_(revisions)))
            db.execute(delete(ActivityRevision).where(ActivityRevision.activity_id.in_(activities)))
            db.execute(delete(Activity).where(Activity.club_id.in_(clubs)))
            db.execute(delete(ClubMembership).where(ClubMembership.club_id.in_(clubs)))
            db.execute(delete(Club).where(Club.school_id.in_(schools)))
            db.execute(delete(AcademicYear).where(AcademicYear.school_id.in_(schools)))
            db.execute(delete(SchoolAdmin).where(SchoolAdmin.school_id.in_(schools)))
            db.execute(update(School).where(School.id.in_(schools)).values(
                closure_teacher_account_id=None, closure_teacher_designation_id=None,
            ))
            db.execute(delete(Account).where(Account.id.in_(data.accounts.values())))
            db.execute(delete(School).where(School.id.in_(schools)))


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def designated(client, scenario):
    response = scenario.designate(client)
    assert response.status_code == 200, response.text


def closure_audits(scenario, action):
    with SessionLocal() as db:
        return list(db.scalars(select(AuditLog).where(
            AuditLog.actor_account_id.in_(scenario.accounts.values()), AuditLog.action == action,
        )))


def year_status(scenario, year="active"):
    with SessionLocal() as db:
        return db.get(AcademicYear, scenario.years[year]).status


def add_activity(db, scenario, *statuses, club_status=ClubStatus.ACTIVE):
    club = Club(
        school_id=scenario.schools["own"], academic_year_id=scenario.years["active"],
        created_by=scenario.accounts["student"], name="Robotics", category="STEM", status=club_status,
    )
    db.add(club)
    db.flush()
    db.add_all([
        ClubMembership(
            club_id=club.id, account_id=scenario.accounts[actor], role=role,
            start_date=START, status=MembershipStatus.ACTIVE,
        )
        for actor, role in (("student", ClubRole.STUDENT_REP), ("teacher", ClubRole.TEACHER_REP))
    ])
    activity = Activity(club_id=club.id, created_by=scenario.accounts["student"])
    db.add(activity)
    db.flush()
    revisions = [
        ActivityRevision(
            activity_id=activity.id, revision_number=number, status=revision_status,
            title=f"Revision {number}", activity_date=START, description="Activity record", category="STEM",
            is_published=revision_status == ActivityRevisionStatus.APPROVED,
        )
        for number, revision_status in enumerate(statuses, 1)
    ]
    db.add_all(revisions)
    db.flush()
    return club.id, activity.id, [revision.id for revision in revisions]


def test_designation_is_school_wide_audited_and_replaceable(client, scenario):
    assert client.get(DESIGNATION, headers=scenario.auth("admin")).json() == {"teacher_account_id": None}
    assert scenario.designate(client).json() == {"teacher_account_id": str(scenario.accounts["teacher"])}
    assert scenario.designate(client).status_code == 200
    assert scenario.designate(client, "replacement").status_code == 200

    response = client.get(DESIGNATION, headers=scenario.auth("admin"))
    assert response.json() == {"teacher_account_id": str(scenario.accounts["replacement"])}
    assert response.headers["cache-control"] == "private, no-store"
    assert client.get(DESIGNATION, headers=scenario.auth("other_admin")).json() == {"teacher_account_id": None}
    audits = closure_audits(scenario, DESIGNATED)
    assert len(audits) == 2
    assert {row.entity_id for row in audits} == {str(scenario.schools["own"])}
    assert {row.details["previous_teacher_account_id"] for row in audits} == {None, str(scenario.accounts["teacher"])}
    assert {row.details["teacher_account_id"] for row in audits} == {
        str(scenario.accounts["teacher"]), str(scenario.accounts["replacement"]),
    }


@pytest.mark.parametrize("teacher", ["student", "inactive", "other_teacher", None])
def test_designation_rejects_invalid_accounts(client, scenario, teacher):
    response = scenario.designate(client, teacher or uuid4())
    assert response.status_code == 404
    assert response.json() == {"detail": "Active teacher not found."}
    assert closure_audits(scenario, DESIGNATED) == []


@pytest.mark.parametrize("actor", [None, "student", "teacher", "owner", "unassigned"])
def test_designation_requires_an_assigned_admin(client, scenario, actor):
    status_code = 401 if actor is None else 403
    assert scenario.designate(client, actor=actor).status_code == status_code
    assert client.get(DESIGNATION, headers=scenario.auth(actor)).status_code == status_code
    assert closure_audits(scenario, DESIGNATED) == []


def test_only_current_teacher_can_initiate_and_idempotency_key_is_required(client, scenario, designated):
    assert scenario.request(client, actor="replacement").status_code == 403
    assert scenario.request(client, actor="inactive").status_code == 401
    assert scenario.request(client, actor="admin").status_code == 403
    assert scenario.request(client, year="historical").status_code == 409
    assert client.post(
        f"{PATH}/{scenario.years['active']}/closure-request", headers=scenario.auth("teacher"),
    ).status_code == 422
    assert scenario.pending(client).json() is None

    response = scenario.request(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["teacher_account_id"] == str(scenario.accounts["teacher"])
    assert body["academic_year_id"] == str(scenario.years["active"])
    assert body["status"] == "PENDING"
    assert datetime.fromisoformat(body["requested_at"]).utcoffset() is not None
    assert body["confirmed_by_account_id"] is body["confirmed_at"] is None
    assert scenario.pending(client).json() == body
    assert year_status(scenario) == AcademicYearStatus.ACTIVE
    audit, = closure_audits(scenario, REQUESTED)
    assert audit.actor_account_id == scenario.accounts["teacher"]
    assert audit.details["request_id"] == body["id"]


def test_request_replays_and_conflicts_are_scoped(client, scenario, designated):
    key = uuid4()
    first = scenario.request(client, key=key)
    assert first.status_code == 201
    assert scenario.request(client, key=key, actor="teacher_tab").json() == first.json()
    assert scenario.request(client, key=key, year="historical").json() == {
        "detail": "Idempotency key reused for another closure request.",
    }
    assert scenario.request(client).status_code == 409
    assert scenario.designate(client, "other_teacher", "other_admin").status_code == 200
    foreign = scenario.request(client, year="other", actor="other_teacher", key=key)
    assert foreign.status_code == 201
    assert foreign.json()["id"] != first.json()["id"]
    assert len(closure_audits(scenario, REQUESTED)) == 2


def test_pending_revisions_in_inactive_clubs_block_initiation(client, scenario, designated):
    with SessionLocal.begin() as db:
        add_activity(db, scenario, ActivityRevisionStatus.PENDING, club_status=ClubStatus.ARCHIVED)
        add_activity(db, scenario, ActivityRevisionStatus.DRAFT, ActivityRevisionStatus.PENDING)
    response = scenario.request(client)
    assert response.status_code == 409
    assert response.json()["detail"]["pending_review_blocker_count"] == 2
    assert scenario.pending(client).json() is None
    assert closure_audits(scenario, REQUESTED) == []


def test_confirmation_and_request_views_validate_school_and_role(client, scenario, designated):
    assert scenario.designate(client, "other_teacher", "other_admin").status_code == 200
    assert scenario.request(client, actor="other_teacher").status_code == 404
    request_id = scenario.request(client).json()["id"]
    assert scenario.confirm(client, request_id, actor="teacher").status_code == 403
    assert scenario.confirm(client, request_id, actor="owner").status_code == 403
    assert scenario.confirm(client, request_id, actor="unassigned").status_code == 403
    assert scenario.confirm(client, request_id, actor="other_admin").status_code == 404
    assert scenario.confirm(client, request_id, year="other", actor="other_admin").status_code == 404
    assert scenario.pending(client, actor="other_admin").status_code == 404
    assert scenario.confirm(client, uuid4()).status_code == 404
    assert scenario.pending(client).json()["status"] == "PENDING"
    assert year_status(scenario) == AcademicYearStatus.ACTIVE
    assert closure_audits(scenario, CLOSED) == []


def test_confirmation_preserves_history_and_closes_only_the_requested_year(client, scenario, designated):
    with SessionLocal.begin() as db:
        club_id, activity_id, revisions = add_activity(
            db, scenario, ActivityRevisionStatus.DRAFT, ActivityRevisionStatus.CHANGES_REQUESTED,
            ActivityRevisionStatus.APPROVED, ActivityRevisionStatus.REJECTED,
        )
        db.add(ActivityAttachment(
            revision_id=revisions[0], file_name="agenda.pdf", file_type="application/pdf", storage_key="test/agenda",
        ))
        for revision_id, decision in zip(revisions[2:], (ActivityReviewDecision.APPROVED, ActivityReviewDecision.REJECTED)):
            db.add(ActivityReview(
                revision_id=revision_id, actor_account_id=scenario.accounts["teacher"],
                review_mode=ReviewMode.DIRECT, decision=decision, comments="Review explanation.",
            ))
        db.add(AuditLog(
            actor_account_id=scenario.accounts["student"], entity_type="activity", entity_id=str(activity_id),
            action="HISTORICAL_TEST_EVENT",
        ))

    def history():
        with SessionLocal() as db:
            return [db.execute(query).all() for query in (
                select(Club.__table__).where(Club.id == club_id),
                select(ClubMembership.__table__).where(ClubMembership.club_id == club_id).order_by(ClubMembership.id),
                select(Activity.__table__).where(Activity.id == activity_id),
                select(ActivityRevision.__table__).where(ActivityRevision.activity_id == activity_id)
                .order_by(ActivityRevision.id),
                select(ActivityReview.__table__).where(ActivityReview.revision_id.in_(revisions)).order_by(ActivityReview.id),
                select(ActivityAttachment.__table__).where(ActivityAttachment.revision_id.in_(revisions)),
                select(AuditLog.__table__).where(AuditLog.entity_id == str(activity_id)),
            )]

    before = history()
    key = uuid4()
    request = scenario.request(client, key=key)
    assert request.status_code == 201, request.text
    request_id = request.json()["id"]
    result = scenario.confirm(client, request_id)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "CONFIRMED"
    assert result.json()["confirmed_by_account_id"] == str(scenario.accounts["admin"])
    assert result.json()["confirmed_at"] is not None
    assert scenario.pending(client).json() == result.json()
    assert scenario.confirm(client, request_id).json() == result.json()
    assert scenario.request(client, key=key).json() == result.json()
    assert scenario.request(client).status_code == 409
    assert history() == before
    assert year_status(scenario) == AcademicYearStatus.CLOSED
    assert year_status(scenario, "other") == AcademicYearStatus.ACTIVE
    with SessionLocal() as db:
        assert db.scalars(select(AcademicYear.status).where(AcademicYear.school_id == scenario.schools["own"])).all() == [
            AcademicYearStatus.CLOSED, AcademicYearStatus.CLOSED,
        ]
    audit, = closure_audits(scenario, CLOSED)
    assert audit.entity_id == str(scenario.years["active"])
    assert audit.details["request_id"] == request_id


def test_confirmation_rechecks_pending_revisions_and_rolls_back(client, scenario, designated):
    request_id = scenario.request(client).json()["id"]
    with SessionLocal.begin() as db:
        add_activity(db, scenario, ActivityRevisionStatus.PENDING, club_status=ClubStatus.ARCHIVED)

    result = scenario.confirm(client, request_id)
    assert result.status_code == 409
    assert result.json()["detail"]["pending_review_blocker_count"] == 1
    assert scenario.pending(client).json()["status"] == "PENDING"
    assert year_status(scenario) == AcademicYearStatus.ACTIVE
    assert closure_audits(scenario, CLOSED) == []


@pytest.mark.parametrize("change", ["replaced", "reappointed", "deactivated"])
def test_confirmation_fails_closed_when_requester_authorization_changed(client, scenario, designated, change):
    request_id = scenario.request(client).json()["id"]
    if change == "deactivated":
        with SessionLocal.begin() as db:
            db.get(Account, scenario.accounts["teacher"]).is_active = False
    else:
        assert scenario.designate(client, "replacement").status_code == 200
        assert scenario.request(client, actor="teacher").status_code == 403
        if change == "reappointed":
            assert scenario.designate(client).status_code == 200

    response = scenario.confirm(client, request_id)
    assert response.status_code == 409
    assert year_status(scenario) == AcademicYearStatus.ACTIVE
    assert scenario.pending(client).json()["status"] == "PENDING"
    assert closure_audits(scenario, CLOSED) == []


def backend_pid(db):
    db.execute(text("SET LOCAL statement_timeout = '12s'"))
    return db.scalar(select(func.pg_backend_pid()))


def concurrent_requests(monkeypatch, *requests):
    ready = Barrier(len(requests), timeout=WAIT)
    pids = Queue()

    def synchronized_db():
        with SessionLocal() as db:
            pids.put(backend_pid(db))
            ready.wait()
            yield db

    def send(request):
        with TestClient(app) as client:
            return request(client)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=len(requests)) as workers:
        patch.setitem(app.dependency_overrides, get_db, synchronized_db)
        futures = [workers.submit(send, request) for request in requests]
        results = [future.result(timeout=WAIT * 2) for future in futures]
    assert len({pids.get_nowait() for _ in requests}) == len(requests)
    return results


def test_concurrent_designations_serialize_with_one_current_teacher(scenario, monkeypatch):
    results = concurrent_requests(
        monkeypatch,
        lambda client: scenario.designate(client),
        lambda client: scenario.designate(client, "replacement", "admin_tab"),
    )
    assert [response.status_code for response in results] == [200, 200]
    audits = closure_audits(scenario, DESIGNATED)
    assert len(audits) == 2
    first, = [audit for audit in audits if audit.details["previous_teacher_account_id"] is None]
    last, = [audit for audit in audits if audit.id != first.id]
    assert last.details["previous_teacher_account_id"] == first.details["teacher_account_id"]
    with SessionLocal() as db:
        school = db.get(School, scenario.schools["own"])
        assert str(school.closure_teacher_account_id) == last.details["teacher_account_id"]


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_requests_create_one_request_and_audit(scenario, designated, monkeypatch, same_key):
    key = uuid4()
    second_key = key if same_key else uuid4()
    results = concurrent_requests(
        monkeypatch,
        lambda client: scenario.request(client, key=key),
        lambda client: scenario.request(client, actor="teacher_tab", key=second_key),
    )
    assert sorted(result.status_code for result in results) == ([201, 201] if same_key else [201, 409])
    if same_key:
        assert results[0].json() == results[1].json()
    assert len(closure_audits(scenario, REQUESTED)) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AcademicYearClosureRequest).where(
            AcademicYearClosureRequest.academic_year_id == scenario.years["active"],
        )) == 1


def test_concurrent_confirmations_record_one_transition(client, scenario, designated, monkeypatch):
    request_id = scenario.request(client).json()["id"]
    first, second = concurrent_requests(
        monkeypatch,
        lambda client: scenario.confirm(client, request_id),
        lambda client: scenario.confirm(client, request_id, actor="admin_tab"),
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["status"] == "CONFIRMED"
    assert year_status(scenario) == AcademicYearStatus.CLOSED
    assert len(closure_audits(scenario, CLOSED)) == 1


def run_transaction(operation, pids, *, release=None):
    with SessionLocal() as db:
        pid = backend_pid(db)
        if release is None:
            pids.put(pid)
        else:
            def pause_before_commit(_session):
                pids.put(pid)
                assert release.wait(timeout=WAIT)

            event.listen(db, "before_commit", pause_before_commit)
        return operation(db)


def ordered_transactions(first_operation, second_operation, error_type):
    first_pids, second_pids, release = Queue(), Queue(), Event()
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(run_transaction, first_operation, first_pids, release=release)
        try:
            first_pid = first_pids.get(timeout=WAIT)
            second = workers.submit(run_transaction, second_operation, second_pids)
            second_pid = second_pids.get(timeout=WAIT)
            assert first_pid != second_pid
            deadline = monotonic() + WAIT
            with SessionLocal() as db:
                while first_pid not in db.scalar(select(func.pg_blocking_pids(second_pid))):
                    assert monotonic() < deadline, "The second transaction bypassed the first transaction's lock"
                    sleep(0.01)
        finally:
            release.set()

        first.result(timeout=WAIT)
        with pytest.raises(error_type) as rejected:
            second.result(timeout=WAIT)
    with SessionLocal() as db:
        assert not db.execute(text(
            "SELECT pid FROM pg_stat_activity WHERE pid = ANY(:pids) "
            "AND pid <> pg_backend_pid() AND xact_start IS NOT NULL"
        ), {"pids": [first_pid, second_pid]}).all()
    return rejected.value


@pytest.mark.parametrize("first_actor", ["student", "admin"])
def test_activity_submission_races_with_actual_confirmation(client, scenario, designated, first_actor):
    request_id = UUID(scenario.request(client).json()["id"])
    with SessionLocal.begin() as db:
        club_id, activity_id, (revision_id,) = add_activity(db, scenario, ActivityRevisionStatus.DRAFT)

    def submit(db):
        student = db.get(Account, scenario.accounts["student"])
        return submit_activity_draft(db, student, club_id, activity_id, revision_id)

    def confirm(db):
        admin = db.get(Account, scenario.accounts["admin"])
        return confirm_year_closure(db, admin, scenario.years["active"], request_id)

    if first_actor == "student":
        error = ordered_transactions(submit, confirm, AcademicYearError)
        assert error.detail["pending_review_blocker_count"] == 1
        assert year_status(scenario) == AcademicYearStatus.ACTIVE
        assert len(closure_audits(scenario, "ACTIVITY_SUBMITTED")) == 1
        assert closure_audits(scenario, CLOSED) == []
        expected = ActivityRevisionStatus.PENDING
    else:
        error = ordered_transactions(confirm, submit, ActivityError)
        assert error.detail == "Academic year is not active."
        assert year_status(scenario) == AcademicYearStatus.CLOSED
        assert closure_audits(scenario, "ACTIVITY_SUBMITTED") == []
        assert len(closure_audits(scenario, CLOSED)) == 1
        expected = ActivityRevisionStatus.DRAFT
    assert error.status_code == 409
    with SessionLocal() as db:
        assert db.get(ActivityRevision, revision_id).status == expected


def test_teacher_waiting_behind_replacement_cannot_request_closure(scenario, designated):
    def replace(db):
        admin = db.get(Account, scenario.accounts["admin"])
        return set_closure_teacher(db, admin, scenario.accounts["replacement"])

    def request(db):
        teacher = db.get(Account, scenario.accounts["teacher"])
        return request_year_closure(db, teacher, scenario.years["active"], uuid4())

    error = ordered_transactions(replace, request, AcademicYearError)
    assert error.status_code == 403
    assert closure_audits(scenario, REQUESTED) == []
    assert year_status(scenario) == AcademicYearStatus.ACTIVE


def test_confirmation_database_failure_rolls_back_year_request_and_audit(client, scenario, designated):
    request_id = scenario.request(client).json()["id"]

    def abort_after_database_flush(db, _context):
        if any(isinstance(row, AuditLog) and row.action == CLOSED for row in db.new):
            db.execute(text("SELECT 1 / 0"))

    event.listen(Session, "after_flush", abort_after_database_flush)
    try:
        result = scenario.confirm(client, request_id)
    finally:
        event.remove(Session, "after_flush", abort_after_database_flush)
    assert result.status_code == 503, result.text
    assert year_status(scenario) == AcademicYearStatus.ACTIVE
    assert scenario.pending(client).json()["status"] == "PENDING"
    assert closure_audits(scenario, CLOSED) == []
    assert scenario.confirm(client, request_id).status_code == 200
    assert len(closure_audits(scenario, CLOSED)) == 1
