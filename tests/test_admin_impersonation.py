from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from queue import Queue
from threading import Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config.database import SessionLocal, get_db
from app.main import app
from app.models.account import Account
from app.models.activity_review import ActivityReview
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
from app.models.auth_session import AuthSession
from app.models.enums import ActivityRevisionStatus, ImpersonationStatus, ReviewMode
from app.models.impersonation_session import ImpersonationSession
from app.models.school import School
from app.models.school_admin import SchoolAdmin
from app.services.sessions import token_hash
from test_academic_year_closure_workflow import (
    WAIT,
    add_activity,
    backend_pid,
    client,
    concurrent_requests,
    scenario,
)
from test_academic_year_history import add_publication


PATH = "/admin/impersonation"
START = f"{PATH}/start"
CURRENT = f"{PATH}/current"
END = f"{PATH}/end"
ELIGIBLE = f"{PATH}/eligible-accounts"
FEED = f"{PATH}/published-activities"
REASON = "Help reproduce the school activity feed view."
STARTED = "IMPERSONATION_STARTED"
ENDED = "IMPERSONATION_ENDED"
INVALIDATED = "IMPERSONATION_INVALIDATED"
VIEWED = "IMPERSONATION_PUBLICATIONS_VIEWED"


@pytest.fixture(autouse=True)
def clean_impersonation_history(scenario):
    yield
    selection = ImpersonationSession.admin_account_id.in_(scenario.accounts.values())
    with SessionLocal.begin() as db:
        db.execute(delete(AuditLog).where(AuditLog.impersonation_session_id.in_(
            select(ImpersonationSession.id).where(selection),
        )))
        db.execute(delete(ImpersonationSession).where(selection))


def start(client, scenario, *, actor="admin", target="teacher", reason=REASON, key=None, headers=None):
    return client.post(START, headers={
        **scenario.auth(actor),
        "Idempotency-Key": str(key or uuid4()),
        **(headers or {}),
    }, json={"target_account_id": str(scenario.accounts.get(target, target)), "reason": reason})


def end(client, scenario, impersonation_id, *, actor="admin", reason=None):
    return client.post(END, headers=scenario.auth(actor), json={
        "impersonation_session_id": str(impersonation_id), "reason": reason,
    })


def bound_session(db, scenario, actor="admin"):
    return db.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash(*scenario.credentials[actor])))


def sessions(scenario):
    with SessionLocal() as db:
        return list(db.scalars(select(ImpersonationSession).where(
            ImpersonationSession.admin_account_id == scenario.accounts["admin"],
        ).order_by(ImpersonationSession.started_at, ImpersonationSession.id)))


def audits(impersonation_id):
    with SessionLocal() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.impersonation_session_id == impersonation_id)
                               .order_by(AuditLog.occurred_at, AuditLog.id)))


def assert_audit_identity(records, scenario, represented="teacher"):
    assert records
    for record in records:
        assert record.actor_account_id == scenario.accounts["admin"]
        assert record.represented_account_id == scenario.accounts[represented]
        assert record.impersonation_session_id is not None
        assert record.occurred_at.tzinfo is not None
        assert record.entity_type and record.entity_id


def assert_no_auth_secrets(response, scenario):
    for forbidden in ("password", "password_hash", "token_hash", "tab_key", "auth_session_id", "@example.com"):
        assert forbidden not in response.text
    assert all(value not in response.text for pair in scenario.credentials.values() for value in pair)


@pytest.mark.parametrize("actor,status", [(None, 401), ("student", 403), ("teacher", 403), ("owner", 403), ("unassigned", 403)])
def test_only_assigned_school_administrators_can_access_lifecycle(client, scenario, actor, status):
    assert start(client, scenario, actor=actor).status_code == status
    assert client.get(ELIGIBLE, headers=scenario.auth(actor)).status_code == status
    assert client.get(CURRENT, headers=scenario.auth(actor)).status_code == status
    assert end(client, scenario, uuid4(), actor=actor).status_code == status
    assert sessions(scenario) == []


@pytest.mark.parametrize("target", ["admin", "unassigned", "inactive", "other_teacher", "other_admin", "owner"])
def test_start_hides_ineligible_targets(client, scenario, target):
    response = start(client, scenario, target=target)
    missing = start(client, scenario, target=uuid4())
    assert response.status_code == missing.status_code == 404
    assert response.json() == missing.json()
    assert sessions(scenario) == []


@pytest.mark.parametrize("target,role", [("student", "STUDENT"), ("teacher", "TEACHER")])
def test_start_binds_to_authenticated_tab_and_preserves_original_identity(client, scenario, target, role):
    with SessionLocal() as db:
        original_auth_id = bound_session(db, scenario).id
        parent_count = db.scalar(select(func.count()).select_from(AuthSession).where(
            AuthSession.account_id.in_(scenario.accounts.values()),
        ))

    result = start(client, scenario, target=target)
    assert result.status_code == 201, result.text
    assert result.headers["Cache-Control"] == "private, no-store"
    assert "set-cookie" not in result.headers
    assert_no_auth_secrets(result, scenario)

    record, = sessions(scenario)
    assert record.admin_account_id == scenario.accounts["admin"]
    assert record.target_account_id == scenario.accounts[target]
    assert record.auth_session_id == original_auth_id
    assert record.school_id == scenario.schools["own"]
    assert record.reason == REASON
    assert record.started_at.tzinfo is not None
    assert record.status == ImpersonationStatus.ACTIVE
    assert record.ended_at is None
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AuthSession).where(
            AuthSession.account_id.in_(scenario.accounts.values()),
        )) == parent_count
        assert db.get(Account, scenario.accounts["admin"]).account_type.value == "SCHOOL_ADMIN"

    current = client.get(CURRENT, headers=scenario.auth("admin"))
    assert current.status_code == 200, current.text
    assert current.json() == result.json()
    assert current.json()["active"] is True
    assert current.json()["represented_role"] == role
    assert current.json()["represented_name"] == target
    assert current.json()["represented_account_id"] == str(scenario.accounts[target])
    assert current.json()["reason"] == REASON
    assert_no_auth_secrets(current, scenario)
    audit, = audits(record.id)
    assert_audit_identity([audit], scenario, target)
    assert audit.action == STARTED
    assert REASON not in str(audit.details)


@pytest.mark.parametrize("payload", [None, "", " \t\r\n "])
def test_start_requires_nonempty_reason_and_valid_tab_key(client, scenario, payload):
    target = str(scenario.accounts["teacher"])
    body = {"target_account_id": target}
    if payload is not None:
        body["reason"] = payload
    headers = {**scenario.auth("admin"), "Idempotency-Key": str(uuid4())}
    assert client.post(START, headers=headers, json=body).status_code == 422
    assert client.post(START, headers=scenario.auth("admin"), json={"target_account_id": target, "reason": REASON}).status_code == 422
    headers["X-ClubOS-Tab"] = scenario.credentials["admin_tab"][1]
    assert client.post(START, headers=headers, json={"target_account_id": target, "reason": REASON}).status_code == 401
    assert sessions(scenario) == []


def test_eligible_accounts_are_school_scoped_bounded_searchable_and_private(client, scenario):
    admin = scenario.auth("admin")
    params = {"limit": 1}
    visible = {}
    while True:
        response = client.get(ELIGIBLE, headers=admin, params=params)
        assert response.status_code == 200, response.text
        assert response.headers["Cache-Control"] == "private, no-store"
        assert_no_auth_secrets(response, scenario)
        page = response.json()
        account, = page["items"]
        assert set(account) == {"account_id", "full_name", "role"}
        assert account["account_id"] not in visible
        visible[account["account_id"]] = account["role"]
        if page["next_cursor"] is None:
            break
        params["after"] = page["next_cursor"]

    assert visible == {
        str(scenario.accounts["student"]): "STUDENT",
        str(scenario.accounts["teacher"]): "TEACHER",
        str(scenario.accounts["replacement"]): "TEACHER",
    }
    found = client.get(ELIGIBLE, headers=admin, params={"q": "TEACHER"})
    assert found.status_code == 200, found.text
    assert [item["account_id"] for item in found.json()["items"]] == [str(scenario.accounts["teacher"])]
    assert client.get(ELIGIBLE, headers=admin, params={"limit": 51}).status_code == 422
    assert client.get(ELIGIBLE, headers=admin, params={"after": "not-a-uuid"}).status_code == 422


def test_start_and_end_retries_are_idempotent_and_cannot_resurrect_or_end_a_later_session(client, scenario):
    key = uuid4()
    created = start(client, scenario, key=key)
    assert created.status_code == 201, created.text
    session_id = created.json()["impersonation_session_id"]
    assert start(client, scenario, key=key).json() == created.json()
    assert start(client, scenario, key=key, target="student").status_code == 409
    assert start(client, scenario, key=key, reason="A different reason").status_code == 409
    assert start(client, scenario, target="student").status_code == 409
    assert start(client, scenario, actor="admin_tab", key=key).status_code == 409
    assert client.get(ELIGIBLE, headers=scenario.auth("admin")).status_code == 403

    ended = end(client, scenario, session_id, reason="Finished checking the feed.")
    assert ended.status_code == 200, ended.text
    assert ended.json()["active"] is False and ended.json()["status"] == "ENDED"
    assert end(client, scenario, session_id, reason="Finished checking the feed.").json() == ended.json()
    assert client.get("/auth/me", headers=scenario.auth("admin")).json()["account_type"] == "SCHOOL_ADMIN"
    assert sessions(scenario)[0].ended_at is not None
    assert sessions(scenario)[0].end_reason == "Finished checking the feed."
    replay = start(client, scenario, key=key)
    assert replay.status_code == 201, replay.text
    assert replay.json()["active"] is False and replay.json()["status"] == "ENDED"
    assert len(sessions(scenario)) == 1

    later = start(client, scenario, target="student")
    assert later.status_code == 201, later.text
    assert later.json()["impersonation_session_id"] != session_id
    assert end(client, scenario, session_id, reason="Finished checking the feed.").status_code == 200
    assert client.get(CURRENT, headers=scenario.auth("admin")).json() == later.json()
    assert [record.action for record in audits(UUID(session_id))] == [STARTED, ENDED]


def test_another_tab_cannot_inherit_or_end_impersonation_by_supplying_its_id(client, scenario):
    created = start(client, scenario)
    assert created.status_code == 201, created.text
    session_id = created.json()["impersonation_session_id"]
    other_tab_headers = {**scenario.auth("admin_tab"), "X-Impersonation-Session-ID": session_id}
    assert client.get(CURRENT, headers=other_tab_headers).json()["active"] is False
    assert client.get(FEED, headers=other_tab_headers, params={"impersonation_session_id": session_id}).status_code == 403
    assert end(client, scenario, session_id, actor="admin_tab").status_code == 404
    assert end(client, scenario, session_id, actor="other_admin").status_code == 404
    assert client.get("/academic-years", headers=other_tab_headers).status_code == 200
    assert client.post("/auth/logout", headers=other_tab_headers).status_code == 204
    assert client.get(CURRENT, headers=scenario.auth("admin")).json() == created.json()


def test_read_allowlist_is_audited_and_uses_the_represented_identity(client, scenario):
    with SessionLocal.begin() as db:
        student = scenario.accounts["student"]
        visible = add_publication(db, scenario.years["active"], student, "Current activity")
        add_publication(db, scenario.years["historical"], student, "Historical activity")
        add_publication(db, scenario.years["other"], scenario.accounts["other_teacher"], "Foreign activity")
        add_publication(db, scenario.years["active"], student, "Private draft", status=ActivityRevisionStatus.DRAFT, published=False)

    assert client.get(FEED, headers=scenario.auth("admin")).status_code == 403
    assert client.get(FEED, headers=scenario.auth("teacher")).status_code == 403
    result = start(client, scenario, target="teacher")
    assert result.status_code == 201, result.text
    feed = client.get(FEED, headers=scenario.auth("admin"), params={"school_id": str(scenario.schools["other"])})
    assert feed.status_code == 200, feed.text
    assert feed.headers["Cache-Control"] == "private, no-store"
    assert_no_auth_secrets(feed, scenario)
    assert [item["revision_id"] for item in feed.json()] == [str(visible.revision_id)]
    assert client.get(FEED, headers=scenario.auth("admin"), params={"limit": 51}).status_code == 422
    record, = sessions(scenario)
    records = audits(record.id)
    assert [entry.action for entry in records] == [STARTED, VIEWED]
    assert_audit_identity(records, scenario)


def test_default_deny_covers_public_and_protected_mutations_and_unapproved_reads(client, scenario):
    with SessionLocal.begin() as db:
        club, activity, (revision,) = add_activity(db, scenario, ActivityRevisionStatus.PENDING)

    result = start(client, scenario)
    assert result.status_code == 201, result.text
    active = scenario.auth("admin")
    idempotent = {**active, "Idempotency-Key": str(uuid4())}
    review_path = f"/clubs/{club}/activities/{activity}/revisions/{revision}/review"
    assert client.post(review_path, headers=idempotent, json={"decision": "APPROVED"}).status_code == 403
    year = scenario.years["active"]
    assert client.post(f"/academic-years/{year}/export", headers=active).status_code == 403
    assert client.get("/academic-years", headers=active).status_code == 403
    assert client.get("/academic-years/history", headers=active).status_code == 403
    assert client.get("/activities/published", headers=active).status_code == 403
    assert client.get("/auth/me", headers=active).status_code == 403
    assert client.get("/clubs/teacher-invitations", headers=active).status_code == 403

    registration = {
        "school_code": "UNUSED-SCHOOL", "full_name": "Example Student", "username": "unusedstudent",
        "email": "unused@example.test", "password": "unused-fixture-input", "account_type": "STUDENT",
    }
    assert client.post("/auth/register", headers=active, json=registration).status_code == 403
    assert client.post("/auth/register-school", headers=active, json={
        "school_name": "Example School", "admin_full_name": "Example Admin", "username": "unusedadmin",
        "email": "unusedadmin@example.test", "password": "unused-fixture-input",
    }).status_code == 403
    assert client.post("/auth/login", headers=active, json={
        "identifier": "unusedstudent", "password": "unused-fixture-input",
    }).status_code == 403
    assert client.post("/auth/heartbeat", headers=active).status_code == 200

    with SessionLocal() as db:
        assert db.get(ActivityRevision, revision).status == ActivityRevisionStatus.PENDING
        assert db.scalar(select(ActivityReview.id).where(ActivityReview.revision_id == revision)) is None

    direct = client.post(review_path, headers={**scenario.auth("teacher"), "Idempotency-Key": str(uuid4())},
                         json={"decision": "APPROVED"})
    assert direct.status_code == 201, direct.text
    with SessionLocal() as db:
        review = db.get(ActivityReview, UUID(direct.json()["review_id"]))
        assert review.actor_account_id == scenario.accounts["teacher"]
        assert review.review_mode == ReviewMode.DIRECT
        assert review.represented_account_id is None
        assert review.impersonation_session_id is None


def test_impersonation_does_not_enable_student_mutations(client, scenario):
    with SessionLocal.begin() as db:
        club, activity, (draft,) = add_activity(db, scenario, ActivityRevisionStatus.DRAFT)
    result = start(client, scenario, target="student")
    assert result.status_code == 201, result.text
    path = f"/clubs/{club}/activities/{activity}/revisions/{draft}/submit"
    assert client.post(path, headers=scenario.auth("admin")).status_code == 403
    with SessionLocal() as db:
        assert db.get(ActivityRevision, draft).status == ActivityRevisionStatus.DRAFT
    direct = client.post(path, headers=scenario.auth("student"))
    assert direct.status_code == 200, direct.text


def test_logout_revokes_parent_and_ends_only_its_impersonation(client, scenario):
    result = start(client, scenario)
    assert result.status_code == 201, result.text
    admin = scenario.auth("admin")
    assert client.post("/auth/logout", headers=admin).status_code == 204
    assert client.post("/auth/logout", headers=admin).status_code == 204
    assert client.get(CURRENT, headers=admin).status_code == 401
    assert client.get(FEED, headers=admin).status_code == 401
    assert start(client, scenario).status_code == 401
    record, = sessions(scenario)
    assert record.status == ImpersonationStatus.ENDED and record.ended_at is not None
    assert record.end_reason == "LOGOUT"
    assert [entry.action for entry in audits(record.id)] == [STARTED, ENDED]
    with SessionLocal() as db:
        assert bound_session(db, scenario).revoked_at is not None
        assert bound_session(db, scenario, "admin_tab").revoked_at is None


@pytest.mark.parametrize("failure,end_reason", [
    ("expired", "PARENT_SESSION_EXPIRED"), ("revoked", "PARENT_SESSION_REVOKED"),
])
def test_invalid_parent_cannot_start_or_use_impersonation(client, scenario, failure, end_reason):
    result = start(client, scenario)
    assert result.status_code == 201, result.text
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as db:
        parent = bound_session(db, scenario)
        if failure == "expired":
            parent.expires_at = now - timedelta(seconds=10)
        else:
            parent.revoked_at = now
    assert client.get(FEED, headers=scenario.auth("admin")).status_code == 401
    assert client.get(CURRENT, headers=scenario.auth("admin")).status_code == 401
    assert start(client, scenario, target="student").status_code == 401
    record, = sessions(scenario)
    assert record.status == ImpersonationStatus.EXPIRED and record.ended_at is not None
    assert record.end_reason == end_reason
    assert [entry.action for entry in audits(record.id)] == [STARTED, INVALIDATED]


@pytest.mark.parametrize("failure,end_reason,denied", [
    ("target_inactive", "TARGET_NO_LONGER_ELIGIBLE", 403),
    ("target_moved", "TARGET_NO_LONGER_ELIGIBLE", 403),
    ("admin_inactive", "ADMIN_AUTHORIZATION_LOST", 401),
    ("school_inactive", "ADMIN_AUTHORIZATION_LOST", 401),
    ("admin_unassigned", "ADMIN_AUTHORIZATION_LOST", 403),
])
def test_changed_account_or_school_cannot_continue_using_impersonation(client, scenario, failure, end_reason, denied):
    result = start(client, scenario)
    assert result.status_code == 201, result.text
    with SessionLocal.begin() as db:
        if failure == "target_inactive":
            db.get(Account, scenario.accounts["teacher"]).is_active = False
        elif failure == "target_moved":
            db.get(Account, scenario.accounts["teacher"]).school_id = scenario.schools["other"]
        elif failure == "admin_inactive":
            db.get(Account, scenario.accounts["admin"]).is_active = False
        elif failure == "school_inactive":
            db.get(School, scenario.schools["own"]).is_active = False
        else:
            db.execute(delete(SchoolAdmin).where(SchoolAdmin.school_id == scenario.schools["own"]))

    assert client.get(FEED, headers=scenario.auth("admin")).status_code == denied
    record, = sessions(scenario)
    assert record.status == ImpersonationStatus.EXPIRED and record.ended_at is not None
    assert record.end_reason == end_reason
    records = audits(record.id)
    assert [entry.action for entry in records] == [STARTED, INVALIDATED]
    assert_audit_identity(records, scenario)


def test_discovering_invalid_target_does_not_fall_back_to_admin_privileges_in_same_request(client, scenario):
    assert start(client, scenario).status_code == 201
    with SessionLocal.begin() as db:
        db.get(Account, scenario.accounts["teacher"]).is_active = False
    assert client.get("/academic-years", headers=scenario.auth("admin")).status_code == 403
    current = client.get(CURRENT, headers=scenario.auth("admin"))
    assert current.status_code == 200, current.text
    assert current.json()["active"] is False and current.json()["status"] == "EXPIRED"
    assert client.get("/academic-years", headers=scenario.auth("admin")).status_code == 200


def test_fresh_tab_can_start_after_expired_parent_and_records_previous_invalidation(client, scenario):
    original = start(client, scenario)
    assert original.status_code == 201, original.text
    with SessionLocal.begin() as db:
        bound_session(db, scenario).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    started = start(client, scenario, actor="admin_tab", target="student")
    assert started.status_code == 201, started.text
    old, new = sessions(scenario)
    assert old.status == ImpersonationStatus.EXPIRED
    assert new.status == ImpersonationStatus.ACTIVE
    assert [entry.action for entry in audits(old.id)] == [STARTED, INVALIDATED]
    assert [entry.action for entry in audits(new.id)] == [STARTED]


def test_renewing_the_parent_session_preserves_binding_and_active_impersonation(client, scenario):
    created = start(client, scenario)
    assert created.status_code == 201, created.text
    with SessionLocal.begin() as db:
        parent = bound_session(db, scenario)
        parent.expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        parent_id = parent.id

    assert client.post("/auth/heartbeat", headers=scenario.auth("admin")).status_code == 200
    assert client.get(CURRENT, headers=scenario.auth("admin")).json() == created.json()
    assert sessions(scenario)[0].auth_session_id == parent_id
    with SessionLocal() as db:
        assert bound_session(db, scenario).expires_at > datetime.now(timezone.utc) + timedelta(hours=11)


def test_database_rejects_an_impersonation_bound_to_someone_elses_parent(scenario):
    with pytest.raises(IntegrityError) as error, SessionLocal.begin() as db:
        db.add(ImpersonationSession(
            admin_account_id=scenario.accounts["admin"], target_account_id=scenario.accounts["teacher"],
            school_id=scenario.schools["own"], auth_session_id=bound_session(db, scenario, "teacher").id,
            reason=REASON, status=ImpersonationStatus.ACTIVE,
        ))
    assert error.value.orig.sqlstate == "23503"


@pytest.mark.parametrize("second_actor,same_key,expected", [
    ("admin", True, [201, 201]), ("admin", False, [201, 409]),
    ("admin_tab", True, [201, 409]), ("admin_tab", False, [201, 409]),
])
def test_concurrent_starts_create_one_session_and_one_audit(scenario, monkeypatch, second_actor, same_key, expected):
    key = uuid4()
    other_key = key if same_key else uuid4()
    first, second = concurrent_requests(
        monkeypatch,
        lambda c: start(c, scenario, key=key),
        lambda c: start(c, scenario, actor=second_actor, key=other_key),
    )
    assert sorted((first.status_code, second.status_code)) == expected
    if expected == [201, 201]:
        assert first.json() == second.json()
    record, = sessions(scenario)
    assert record.status == ImpersonationStatus.ACTIVE
    assert [entry.action for entry in audits(record.id)] == [STARTED]


def test_concurrent_end_retries_record_one_transition(client, scenario, monkeypatch):
    created = start(client, scenario)
    assert created.status_code == 201, created.text
    session_id = created.json()["impersonation_session_id"]
    first, second = concurrent_requests(
        monkeypatch, lambda c: end(c, scenario, session_id), lambda c: end(c, scenario, session_id),
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert [entry.action for entry in audits(UUID(session_id))] == [STARTED, ENDED]


def ordered_requests(monkeypatch, first, second, *, pause_at="before_commit"):
    first_pids, second_pids = Queue(), Queue()
    release = Event()

    def controlled_db(request: Request):
        with SessionLocal() as db:
            pid = backend_pid(db)
            if request.headers["X-ClubOS-Test-Request"] == "first":
                def hold_first_request(_session):
                    first_pids.put_nowait(pid)
                    assert release.wait(timeout=WAIT)

                event.listen(db, pause_at, hold_first_request, once=True)
            else:
                second_pids.put_nowait(pid)
            yield db

    def send(label, operation):
        with TestClient(app, headers={"X-ClubOS-Test-Request": label}) as client:
            return operation(client)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as workers:
        patch.setitem(app.dependency_overrides, get_db, controlled_db)
        first_request = workers.submit(send, "first", first)
        try:
            first_pid = first_pids.get(timeout=WAIT)
            second_request = workers.submit(send, "second", second)
            second_pid = second_pids.get(timeout=WAIT)
            assert first_pid != second_pid
            if pause_at == "after_commit":
                second_request.result(timeout=WAIT)
            else:
                deadline = monotonic() + WAIT
                with SessionLocal() as db:
                    while first_pid not in db.scalar(select(func.pg_blocking_pids(second_pid))):
                        assert not second_request.done(), "The second request bypassed the first request's lock."
                        assert monotonic() < deadline, "The second request did not wait for the first request's lock."
                        sleep(0.01)
        finally:
            release.set()

        results = first_request.result(timeout=WAIT), second_request.result(timeout=WAIT)

    with SessionLocal() as db:
        assert not db.execute(text(
            "SELECT pid FROM pg_stat_activity WHERE pid = ANY(:pids) "
            "AND pid <> pg_backend_pid() AND xact_start IS NOT NULL"
        ), {"pids": [first_pid, second_pid]}).all()
    return results


def test_logout_after_renewal_commit_prevents_allowlisted_read(client, scenario, monkeypatch):
    created = start(client, scenario)
    assert created.status_code == 201, created.text
    session_id = UUID(created.json()["impersonation_session_id"])
    with SessionLocal.begin() as db:
        bound_session(db, scenario).expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)

    denied, logged_out = ordered_requests(
        monkeypatch,
        lambda c: c.get(FEED, headers=scenario.auth("admin")),
        lambda c: c.post("/auth/logout", headers=scenario.auth("admin")),
        pause_at="after_commit",
    )
    assert logged_out.status_code == 204, logged_out.text
    assert denied.status_code == 401, denied.text
    assert [entry.action for entry in audits(session_id)] == [STARTED, ENDED]


@pytest.mark.parametrize("first_action", ["start", "logout"])
def test_start_and_logout_serialize_on_the_parent_session(scenario, monkeypatch, first_action):
    def initiate(client):
        return start(client, scenario)

    def logout(client):
        return client.post("/auth/logout", headers=scenario.auth("admin"))

    if first_action == "start":
        created, logged_out = ordered_requests(monkeypatch, initiate, logout)
        assert (created.status_code, logged_out.status_code) == (201, 204)
        record, = sessions(scenario)
        assert record.status == ImpersonationStatus.ENDED
        assert [entry.action for entry in audits(record.id)] == [STARTED, ENDED]
    else:
        logged_out, denied = ordered_requests(monkeypatch, logout, initiate)
        assert (logged_out.status_code, denied.status_code) == (204, 401)
        assert sessions(scenario) == []


@pytest.mark.parametrize("first_action", ["read", "end"])
def test_end_and_authorized_read_serialize_and_recheck_context(client, scenario, monkeypatch, first_action):
    created = start(client, scenario)
    assert created.status_code == 201, created.text
    session_id = created.json()["impersonation_session_id"]

    def read(client):
        return client.get(FEED, headers=scenario.auth("admin"))

    def terminate(client):
        return end(client, scenario, session_id)

    if first_action == "read":
        read_result, ended = ordered_requests(monkeypatch, read, terminate)
        assert (read_result.status_code, ended.status_code) == (200, 200)
        assert [entry.action for entry in audits(UUID(session_id))] == [STARTED, VIEWED, ENDED]
    else:
        ended, read_result = ordered_requests(monkeypatch, terminate, read)
        assert (ended.status_code, read_result.status_code) == (200, 403)
        assert [entry.action for entry in audits(UUID(session_id))] == [STARTED, ENDED]


@pytest.mark.parametrize("operation,action", [("start", STARTED), ("view", VIEWED), ("end", ENDED)])
def test_database_failure_rolls_back_session_and_audit_and_withholds_unaudited_result(client, scenario, operation, action):
    if operation != "start":
        created = start(client, scenario)
        assert created.status_code == 201, created.text
        session_id = UUID(created.json()["impersonation_session_id"])
    key = uuid4()

    def request():
        if operation == "start":
            return start(client, scenario, key=key)
        if operation == "end":
            return end(client, scenario, session_id)
        return client.get(FEED, headers=scenario.auth("admin"))

    def fail_after_flush(db, _context):
        if any(isinstance(row, AuditLog) and row.action == action for row in db.new):
            db.execute(text("SELECT 1 / 0"))

    event.listen(Session, "after_flush", fail_after_flush)
    try:
        failed = request()
    finally:
        event.remove(Session, "after_flush", fail_after_flush)
    assert failed.status_code == 503, failed.text
    if operation == "start":
        assert sessions(scenario) == []
    else:
        assert sessions(scenario)[0].status == ImpersonationStatus.ACTIVE
        assert [entry.action for entry in audits(session_id)] == [STARTED]
    assert request().status_code == (201 if operation == "start" else 200)
