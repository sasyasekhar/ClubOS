from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from queue import Queue
from threading import Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.orm import Session

from app.config.database import SessionLocal, get_db
from app.main import app
from app.models.account import Account
from app.models.activity_deadline_rejection import ActivityDeadlineRejection
from app.models.activity_review import ActivityReview
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.enums import AccountType, ActivityRevisionStatus, ClubRole, ClubStatus, MembershipStatus, ReviewMode
from app.models.representative_application import RepresentativeApplication
from app.services.academic_year_closure_finalization import finalize_expired_closure_window
from app.services.revision_retention import purge_expired_superseded_content
from app.services.storage import get_attachment_storage
from app.services.storage.memory import MemoryAttachmentStorage
from attachment_samples import sample_pdf
from test_academic_year_closure_workflow import START, WAIT, add_activity, backend_pid, client, scenario, set_request_deadline
from test_academic_year_exports import read_export
from test_academic_year_history import Publication, add_archived_year, add_attachment, add_publication
from test_admin_impersonation import (
    CURRENT, ENDED, FEED, INVALIDATED, PATH, REASON, STARTED, assert_audit_identity, assert_no_auth_secrets,
    audits, clean_impersonation_history, clock, end, ordered_requests, start,
)


DASHBOARD = f"{PATH}/dashboard"
REVIEWED = "ACTIVITY_REVIEWED"


def revision_url(revision):
    return f"/clubs/{revision.club_id}/activities/{revision.activity_id}/revisions/{revision.revision_id}"


def add_private_activity(db, scenario, *statuses):
    club_id, activity_id, revisions = add_activity(db, scenario, *statuses)
    return tuple(Publication(club_id, activity_id, revision_id) for revision_id in revisions)


@pytest.fixture
def pending(scenario):
    with SessionLocal.begin() as db:
        pending, = add_private_activity(db, scenario, ActivityRevisionStatus.PENDING)
        return pending


@pytest.fixture
def represented_teacher(client, scenario):
    created = start(client, scenario)
    assert created.status_code == 201, created.text
    return UUID(created.json()["impersonation_session_id"])


def review(client, scenario, target, session_id, *, decision="APPROVED", comments=None, key=None, actor="admin"):
    payload = {"decision": decision, "comments": comments}
    if session_id is not None:
        payload["impersonation_session_id"] = str(session_id)
    return client.post(
        f"{PATH if session_id is not None else ''}{revision_url(target)}/review",
        headers={**scenario.auth(actor), "Idempotency-Key": str(key or uuid4())},
        json=payload,
    )


def review_records(revision):
    with SessionLocal() as db:
        reviews = db.scalars(select(ActivityReview).where(ActivityReview.revision_id == revision.revision_id)).all()
        events = db.scalars(select(AuditLog).where(
            AuditLog.entity_id == str(revision.revision_id), AuditLog.action == REVIEWED,
        )).all()
        return reviews, events


def assert_unreviewed(revision):
    assert review_records(revision) == ([], [])
    with SessionLocal() as db:
        assert db.get(ActivityRevision, revision.revision_id).status == ActivityRevisionStatus.PENDING


@pytest.fixture
def applications(scenario):
    with SessionLocal.begin() as db:
        source = add_publication(db, scenario.years["historical"], scenario.accounts["student"], "Historical activity")
        carried = Club(
            school_id=scenario.schools["own"], academic_year_id=scenario.years["active"], name="Carried club",
            description="Related club details", category="STEM", status=ClubStatus.PENDING,
            rolled_over_from_club_id=source.club_id, rollover_batch_id=uuid4(),
        )
        other_student = Account(
            school_id=scenario.schools["own"], account_type=AccountType.STUDENT, full_name="Another student",
            username=f"student_{uuid4().hex}", email=f"{uuid4().hex}@example.com", password_hash="test-only", is_active=True,
        )
        db.add_all([carried, other_student])
        db.flush()
        scenario.accounts["another_student"] = other_student.id
        rows = [RepresentativeApplication(
            school_id=carried.school_id, club_id=carried.id, applicant_account_id=applicant,
        ) for applicant in (scenario.accounts["student"], other_student.id)]
        db.add_all(rows)
        db.flush()
        own_id, other_id = (row.id for row in rows)
        club_id = carried.id
    try:
        yield own_id, other_id, club_id
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(RepresentativeApplication).where(RepresentativeApplication.id.in_((own_id, other_id))))


def test_student_dashboard_is_paginated_and_limited_to_student_access(client, scenario, applications):
    own_application, other_application, related_club = applications
    with SessionLocal.begin() as db:
        draft, pending = add_private_activity(db, scenario, ActivityRevisionStatus.DRAFT, ActivityRevisionStatus.PENDING)
        published = add_publication(db, scenario.years["active"], scenario.accounts["replacement"], "School event")
        unrelated = add_publication(
            db, scenario.years["active"], scenario.accounts["replacement"], "Private same-school draft",
            status=ActivityRevisionStatus.DRAFT, published=False,
        )
        outside = add_publication(db, scenario.years["other"], scenario.accounts["other_teacher"], "Other school's event")

    started = start(client, scenario, target="student")
    admin = scenario.auth("admin")
    response = client.get(DASHBOARD, headers=admin)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    assert_no_auth_secrets(response, scenario)
    assert response.json() == client.get("/dashboard", headers=scenario.auth("student")).json()
    result = response.json()
    assert result["role"] == "STUDENT"
    assert {row["club_id"] for row in result["memberships"]["items"]} == {str(draft.club_id)}
    assert {row["id"] for row in result["representative_applications"]["items"]} == {str(own_application)}
    assert str(other_application) not in response.text
    assert {row["revision_id"] for row in result["activity_revisions"]["items"]} == {
        str(draft.revision_id), str(pending.revision_id),
    }
    assert [row["revision_id"] for row in result["published_activities"]] == [str(published.revision_id)]
    assert "Private same-school draft" not in response.text and str(outside.revision_id) not in response.text

    page = client.get(DASHBOARD, headers=admin, params={"limit": 1}).json()["activity_revisions"]
    after = client.get(DASHBOARD, headers=admin, params={"limit": 1, "activities_after": page["next_cursor"]})
    last = after.json()["activity_revisions"]
    assert last["next_cursor"] is None and last["items"] != page["items"]
    assert client.get(DASHBOARD, headers=admin, params={"limit": 51}).status_code == 422

    assert client.get(f"{DASHBOARD}/clubs/{related_club}", headers=admin).json()["description"] == "Related club details"
    assert client.get(f"{PATH}{revision_url(draft)}", headers=admin).json()["status"] == "DRAFT"
    for hidden in (unrelated, outside):
        assert client.get(f"{DASHBOARD}/clubs/{hidden.club_id}", headers=admin).status_code == 404
        assert client.get(f"{PATH}{revision_url(hidden)}", headers=admin).status_code == 404
    events = audits(UUID(started.json()["impersonation_session_id"]))
    assert_audit_identity(events, scenario, "student")
    assert [entry.action for entry in events].count("IMPERSONATION_DASHBOARD_VIEWED") == 3
    dashboard_audit = next(entry for entry in events if entry.action == "IMPERSONATION_DASHBOARD_VIEWED")
    assert dashboard_audit.entity_id == str(scenario.accounts["student"])


def test_teacher_dashboard_shows_assigned_queue_invitations_and_authorized_attachments(client, scenario, monkeypatch):
    storage = MemoryAttachmentStorage()
    monkeypatch.setitem(app.dependency_overrides, get_attachment_storage, lambda: storage)
    with SessionLocal.begin() as db:
        draft, pending = add_private_activity(db, scenario, ActivityRevisionStatus.DRAFT, ActivityRevisionStatus.PENDING)
        hidden = add_publication(
            db, scenario.years["active"], scenario.accounts["student"], "Unassigned review",
            status=ActivityRevisionStatus.PENDING, published=False,
        )
        outside = add_publication(db, scenario.years["other"], scenario.accounts["other_teacher"], "Foreign school")
        published = add_publication(db, scenario.years["active"], scenario.accounts["student"], "Published event")
        invitation = Club(
            school_id=scenario.schools["own"], academic_year_id=scenario.years["active"],
            created_by=scenario.accounts["student"], name="Inviting club", category="Arts", status=ClubStatus.PENDING,
        )
        withdrawn = Club(
            school_id=invitation.school_id, academic_year_id=invitation.academic_year_id, created_by=invitation.created_by,
            name="Withdrawn invitation", category="Arts", status=ClubStatus.REJECTED,
        )
        db.add_all([invitation, withdrawn])
        db.flush()
        invitation_id, withdrawn_id = invitation.id, withdrawn.id
        db.add_all(ClubMembership(
            club_id=club_id, account_id=scenario.accounts["teacher"], role=ClubRole.TEACHER_REP,
            status=MembershipStatus.PENDING, start_date=START,
        ) for club_id in (invitation_id, withdrawn_id))
        attachment_path = add_attachment(db, storage, pending)
        hidden_paths = [add_attachment(db, storage, row) for row in (draft, hidden, outside)]

    started = start(client, scenario)
    admin = scenario.auth("admin")
    response = client.get(DASHBOARD, headers=admin)
    assert response.status_code == 200, response.text
    assert_no_auth_secrets(response, scenario)
    assert response.json() == client.get("/dashboard", headers=scenario.auth("teacher")).json()
    result = response.json()
    assert result["role"] == "TEACHER"
    assert {row["club_id"] for row in result["memberships"]["items"]} == {str(draft.club_id)}
    assert [row["club_id"] for row in result["teacher_invitations"]["items"]] == [str(invitation_id)]
    assert [row["revision_id"] for row in result["pending_reviews"]["items"]] == [str(pending.revision_id)]
    assert [row["revision_id"] for row in result["published_activities"]] == [str(published.revision_id)]
    assert client.get(f"{DASHBOARD}/clubs/{invitation_id}", headers=admin).status_code == 200
    assert client.get(f"{DASHBOARD}/clubs/{hidden.club_id}", headers=admin).status_code == 404
    assert client.get(f"{DASHBOARD}/clubs/{withdrawn_id}", headers=admin).status_code == 404

    detail = client.get(f"{PATH}{revision_url(pending)}", headers=admin)
    assert detail.status_code == 200, detail.text
    metadata, = detail.json()["attachments"]["items"]
    assert metadata == {
        "id": attachment_path.rsplit("/", 1)[-1], "revision_id": str(pending.revision_id),
        "file_name": "report.pdf", "file_type": "application/pdf",
    }
    download = client.get(f"{PATH}{attachment_path}", headers=admin)
    assert download.status_code == 200 and download.content == sample_pdf()
    assert download.headers["Cache-Control"] == "private, no-store"
    assert download.headers["X-Content-Type-Options"] == "nosniff"
    for path in hidden_paths:
        assert client.get(f"{PATH}{path}", headers=admin).status_code == 404

    events = audits(UUID(started.json()["impersonation_session_id"]))
    assert_audit_identity(events, scenario)
    assert [entry.action for entry in events] == [
        STARTED, "IMPERSONATION_DASHBOARD_VIEWED", "IMPERSONATION_CLUB_VIEWED", "IMPERSONATION_REVISION_VIEWED",
        "IMPERSONATION_ATTACHMENT_DOWNLOADED",
    ]
    assert events[-1].entity_id == metadata["id"]


@pytest.mark.parametrize("decision,comments", [
    ("APPROVED", None), ("REJECTED", "Supply the activity plan."), ("CHANGES_REQUESTED", "Update the date."),
])
def test_impersonated_teacher_reviews_preserve_attribution_and_safe_history(
    client, scenario, pending, clock, represented_teacher, decision, comments,
):
    before = client.get(CURRENT, headers=scenario.auth("admin")).json()
    clock.now += timedelta(minutes=2)
    result = review(client, scenario, pending, represented_teacher, decision=decision, comments=comments)
    assert result.status_code == 201, result.text
    assert result.json()["status"] == decision
    current = client.get(CURRENT, headers=scenario.auth("admin")).json()
    assert current["expires_at"] == before["expires_at"] and current["remaining_seconds"] == 780

    records, events = review_records(pending)
    record, = records
    event, = events
    assert record.actor_account_id == scenario.accounts["admin"]
    assert record.represented_account_id == scenario.accounts["teacher"]
    assert record.impersonation_session_id == represented_teacher
    assert record.review_mode == ReviewMode.IMPERSONATION
    assert (record.decision.value, record.comments, record.reviewed_at) == (decision, comments, clock.now)
    assert_audit_identity(events, scenario)
    assert event.impersonation_session_id == represented_teacher and event.occurred_at == record.reviewed_at
    assert event.details == {"review_id": str(record.id), "decision": decision}
    feed = client.get(FEED, headers=scenario.auth("admin")).json()
    assert [row["revision_id"] for row in feed] == ([str(pending.revision_id)] if decision == "APPROVED" else [])

    student_history = client.get(revision_url(pending), headers=scenario.auth("student"))
    teacher_history = client.get(DASHBOARD, headers=scenario.auth("admin")).json()["review_history"]["items"]
    assert student_history.status_code == 200, student_history.text
    history, = student_history.json()["reviews"]["items"]
    assert history["review_mode"] == "IMPERSONATION" and history["comments"] == comments
    assert teacher_history == [history]
    for private in (REASON, str(represented_teacher), str(scenario.accounts["admin"]), str(scenario.accounts["teacher"])):
        assert private not in student_history.text


@pytest.mark.parametrize("decision", ["REJECTED", "CHANGES_REQUESTED"])
def test_required_explanation_applies_to_impersonated_reviews(client, scenario, pending, represented_teacher, decision):
    denied = review(client, scenario, pending, represented_teacher, decision=decision, comments="  ")
    assert denied.status_code == 422, denied.text
    assert_unreviewed(pending)


@pytest.mark.parametrize("actor,expected", [(None, 401), ("admin_tab", 403), ("teacher", 403), ("other_admin", 403)])
def test_session_identifier_does_not_grant_authority(client, scenario, pending, represented_teacher, actor, expected):
    denied = review(client, scenario, pending, represented_teacher, actor=actor)
    assert denied.status_code == expected, denied.text
    assert_unreviewed(pending)


@pytest.mark.parametrize("target", ["student", "replacement", "deactivated_teacher"])
def test_ineligible_represented_accounts_cannot_review(client, scenario, pending, target):
    started = start(client, scenario, target="teacher" if target == "deactivated_teacher" else target)
    assert started.status_code == 201, started.text
    if target == "deactivated_teacher":
        with SessionLocal.begin() as db:
            db.get(Account, scenario.accounts["teacher"]).is_active = False
    denied = review(client, scenario, pending, started.json()["impersonation_session_id"])
    assert denied.status_code == 403, denied.text
    assert_unreviewed(pending)


def test_review_enforces_the_school_and_all_resource_relationships(client, scenario, pending, represented_teacher):
    with SessionLocal.begin() as db:
        other = add_publication(
            db, scenario.years["other"], scenario.accounts["other_teacher"], "Foreign pending activity",
            status=ActivityRevisionStatus.PENDING, published=False,
        )
        mismatched, = add_private_activity(db, scenario, ActivityRevisionStatus.PENDING)
    targets = (
        other,
        Publication(pending.club_id, mismatched.activity_id, mismatched.revision_id),
        Publication(pending.club_id, pending.activity_id, mismatched.revision_id),
    )
    for target in targets:
        denied = review(client, scenario, target, represented_teacher)
        assert denied.status_code == 404, denied.text
    for row in (other, mismatched, pending):
        assert_unreviewed(row)


@pytest.mark.parametrize("year", ["closed", "archived", "deadline"])
def test_review_enforces_the_academic_year_gate(client, scenario, pending, represented_teacher, year):
    if year == "deadline":
        assert scenario.designate(client, actor="admin_tab").status_code == 200
        requested = scenario.request(client)
        assert requested.status_code == 201, requested.text
        set_request_deadline(requested.json()["id"])
    else:
        with SessionLocal.begin() as db:
            year_id = scenario.years["historical"] if year == "closed" else add_archived_year(db, scenario.schools["own"])
            db.get(Club, pending.club_id).academic_year_id = year_id
    denied = review(client, scenario, pending, represented_teacher)
    assert denied.status_code == 409, denied.text
    if year == "deadline":
        assert denied.json()["detail"]["code"] == "ACADEMIC_YEAR_COMPLETION_WINDOW_ENDED"
    assert_unreviewed(pending)


def test_review_requires_pending_status_and_both_uuid_inputs(client, scenario, represented_teacher):
    with SessionLocal.begin() as db:
        draft, = add_private_activity(db, scenario, ActivityRevisionStatus.DRAFT)
    url = f"{PATH}{revision_url(draft)}/review"
    admin = scenario.auth("admin")
    payload = {"decision": "APPROVED", "impersonation_session_id": str(represented_teacher)}
    assert client.post(url, headers=admin, json=payload).status_code == 422
    assert client.post(url, headers={**admin, "Idempotency-Key": "invalid"}, json=payload).status_code == 422
    key = {**admin, "Idempotency-Key": str(uuid4())}
    assert client.post(url, headers=key, json={"decision": "APPROVED"}).status_code == 422
    assert client.post(url, headers=key, json={**payload, "impersonation_session_id": "invalid"}).status_code == 422
    assert client.post(url, headers=key, json=payload).status_code == 409
    assert review_records(draft) == ([], [])


@pytest.mark.parametrize("target", ["student", "teacher"])
def test_ordinary_business_routes_still_block_impersonation(client, scenario, pending, target):
    assert start(client, scenario, target=target).status_code == 201
    admin = {**scenario.auth("admin"), "Idempotency-Key": str(uuid4())}
    club = f"/clubs/{pending.club_id}"
    rev = revision_url(pending)
    year = f"/academic-years/{scenario.years['active']}"
    draft = {"title": "Club meeting", "activity_date": START.isoformat(), "description": "Meeting", "category": "STEM"}
    club_request = {"name": "Robotics", "category": "STEM", "teacher_account_id": str(scenario.accounts["teacher"])}
    writes = [
        ("POST", f"{club}/activities", draft), ("PUT", rev, draft), ("POST", f"{rev}/submit", None),
        ("POST", f"{rev}/review", {"decision": "APPROVED"}),
        ("POST", "/clubs", club_request),
        ("POST", f"{club}/reviews", {"decision": "APPROVED"}),
        ("POST", f"{club}/teacher-invitation", {"decision": "ACCEPT"}),
        ("POST", f"{club}/teacher-invitation", {"decision": "DECLINE"}),
        ("POST", f"{club}/representative-applications", None),
        ("POST", f"{club}/memberships/{uuid4()}/end", {"reason": "Term ended"}),
        ("POST", f"{year}/closure-request", None),
        ("POST", f"{year}/confirm-closure", {"request_id": str(uuid4())}),
        ("POST", f"{year}/archive", {"export_saved": True}),
        ("POST", f"{year}/rollover-clubs", {"source_club_ids": [str(pending.club_id)]}),
    ]
    for method, path, payload in writes:
        denied = client.request(method, path, headers=admin, json=payload)
        assert denied.status_code == 403, (method, path, denied.text)
    denied_upload = client.post(
        f"{rev}/attachments", headers=admin, files={"file": ("report.pdf", sample_pdf(), "application/pdf")},
    )
    assert denied_upload.status_code == 403, denied_upload.text
    assert_unreviewed(pending)


@pytest.mark.parametrize("termination", ["end", "expire"])
def test_stale_review_cannot_act_in_a_new_session(client, scenario, pending, represented_teacher, clock, termination):
    if termination == "end":
        assert end(client, scenario, represented_teacher).status_code == 200
    else:
        clock.now = datetime.fromisoformat(client.get(CURRENT, headers=scenario.auth("admin")).json()["expires_at"])
        assert review(client, scenario, pending, represented_teacher).status_code == 403
    later = start(client, scenario)
    assert later.status_code == 201, later.text
    assert review(client, scenario, pending, represented_teacher).status_code == 409
    assert_unreviewed(pending)
    assert review(client, scenario, pending, later.json()["impersonation_session_id"]).status_code == 201


def test_replacement_review_retains_the_superseded_revision_for_thirty_days(client, scenario, represented_teacher):
    with SessionLocal.begin() as db:
        previous, replacement = add_private_activity(db, scenario, ActivityRevisionStatus.APPROVED, ActivityRevisionStatus.PENDING)

    result = review(client, scenario, replacement, represented_teacher)
    assert result.status_code == 201, result.text
    review_record, = review_records(replacement)[0]
    with SessionLocal() as db:
        old = db.get(ActivityRevision, previous.revision_id)
        new = db.get(ActivityRevision, replacement.revision_id)
        assert not old.is_published and new.is_published
        assert old.superseded_at == review_record.reviewed_at
        purge_at = old.superseded_at + timedelta(days=30)
        db.rollback()
        storage = MemoryAttachmentStorage()
        purge_expired_superseded_content(db, storage=storage, now=purge_at - timedelta(microseconds=1))
        assert db.get(ActivityRevision, previous.revision_id).content_purged_at is None
        db.rollback()
        purge_expired_superseded_content(db, storage=storage, now=purge_at)
        assert db.get(ActivityRevision, previous.revision_id).content_purged_at == purge_at
        assert db.get(ActivityReview, review_record.id).impersonation_session_id == represented_teacher


def test_review_idempotency_conflicts_and_year_export_provenance(client, scenario, pending, represented_teacher):
    with SessionLocal.begin() as db:
        second, third = add_private_activity(db, scenario, ActivityRevisionStatus.PENDING, ActivityRevisionStatus.PENDING)
    key = uuid4()
    first = review(client, scenario, pending, represented_teacher, key=key)
    assert first.status_code == 201, first.text
    assert review(client, scenario, pending, represented_teacher, key=key).json() == first.json()
    assert review(client, scenario, pending, represented_teacher, key=key, comments="Different comment").status_code == 409
    assert review(client, scenario, second, represented_teacher, key=key).status_code == 409
    assert_unreviewed(second)
    direct = review(client, scenario, third, None, actor="teacher", key=key)
    assert direct.status_code == 201, direct.text
    direct_record, = review_records(third)[0]
    assert direct_record.actor_account_id == scenario.accounts["teacher"] and direct_record.review_mode == ReviewMode.DIRECT
    assert direct_record.represented_account_id is None and direct_record.impersonation_session_id is None

    response = client.post(f"/academic-years/{scenario.years['active']}/export", headers=scenario.auth("admin_tab"))
    rows, _ = read_export(response)
    assert {row["review_id"]: row["review_mode"] for row in rows["activity_reviews.csv"]} == {
        first.json()["review_id"]: "IMPERSONATION", direct.json()["review_id"]: "DIRECT",
    }


def test_failed_publication_rolls_back_both_revisions_review_and_audit(client, scenario, represented_teacher):
    with SessionLocal.begin() as db:
        previous, replacement = add_private_activity(db, scenario, ActivityRevisionStatus.APPROVED, ActivityRevisionStatus.PENDING)

    def fail_review(db, _context):
        if any(isinstance(row, AuditLog) and row.action == REVIEWED for row in db.new):
            db.execute(text("SELECT 1 / 0"))

    event.listen(Session, "after_flush", fail_review)
    try:
        result = review(client, scenario, replacement, represented_teacher)
    finally:
        event.remove(Session, "after_flush", fail_review)

    assert result.status_code == 503, result.text
    assert_unreviewed(replacement)
    with SessionLocal() as db:
        old = db.get(ActivityRevision, previous.revision_id)
        assert old.is_published and old.superseded_at is None
    assert review(client, scenario, replacement, represented_teacher).status_code == 201


@pytest.mark.parametrize("winner", ["teacher", "admin"])
def test_direct_and_impersonated_review_race_with_one_decision(scenario, pending, represented_teacher, monkeypatch, winner):
    def direct(c):
        return review(c, scenario, pending, None, actor="teacher", decision="REJECTED", comments="Incomplete plan.")

    def impersonated(c):
        return review(c, scenario, pending, represented_teacher)

    first, second = (direct, impersonated) if winner == "teacher" else (impersonated, direct)
    approved, denied = ordered_requests(monkeypatch, first, second)
    assert (approved.status_code, denied.status_code) == (201, 409)
    records, events = review_records(pending)
    record, = records
    event, = events
    assert record.actor_account_id == event.actor_account_id == scenario.accounts[winner]
    assert record.review_mode == (ReviewMode.DIRECT if winner == "teacher" else ReviewMode.IMPERSONATION)
    assert record.decision.value == approved.json()["decision"]


@pytest.mark.parametrize("conflict", [False, True])
def test_concurrent_impersonated_review_retries_produce_one_review_and_audit(
    scenario, pending, represented_teacher, monkeypatch, conflict,
):
    key = uuid4()
    options = {"decision": "REJECTED", "comments": "Incomplete plan."} if conflict else {}
    first, second = ordered_requests(
        monkeypatch,
        lambda c: review(c, scenario, pending, represented_teacher, key=key),
        lambda c: review(c, scenario, pending, represented_teacher, key=key, **options),
    )
    assert (first.status_code, second.status_code) == (201, 409 if conflict else 201)
    if not conflict:
        assert first.json() == second.json()
    records, events = review_records(pending)
    record, = records
    audit, = events
    assert record.decision.value == "APPROVED" and audit.impersonation_session_id == represented_teacher


@pytest.mark.parametrize("termination", ["end", "logout"])
@pytest.mark.parametrize("review_first", [False, True])
def test_review_serializes_with_termination_and_parent_logout(
    scenario, pending, represented_teacher, monkeypatch, termination, review_first,
):
    def mutate(c):
        return review(c, scenario, pending, represented_teacher)

    def terminate(c):
        if termination == "end":
            return end(c, scenario, represented_teacher)
        return c.post("/auth/logout", headers=scenario.auth("admin"))

    if review_first:
        result, stopped = ordered_requests(monkeypatch, mutate, terminate)
        assert result.status_code == 201, result.text
    else:
        stopped, result = ordered_requests(monkeypatch, terminate, mutate)
        assert result.status_code == (403 if termination == "end" else 401), result.text
        assert_unreviewed(pending)
    assert stopped.status_code == (200 if termination == "end" else 204), stopped.text
    expected = [STARTED, REVIEWED, ENDED] if review_first else [STARTED, ENDED]
    assert [row.action for row in audits(represented_teacher)] == expected


def review_waiting_on_transaction(monkeypatch, operation, review_request, while_waiting):
    pids, waiters = Queue(), Queue()
    release = Event()

    def hold_transaction():
        with SessionLocal() as db:
            def hold_before_commit(_session):
                pids.put_nowait(backend_pid(db))
                assert release.wait(timeout=WAIT)

            event.listen(db, "before_commit", hold_before_commit, once=True)
            return operation(db)

    def track_http_connection():
        with SessionLocal() as db:
            waiters.put_nowait(backend_pid(db))
            yield db

    def send_review():
        with TestClient(app) as http_client:
            return review_request(http_client)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as workers:
        patch.setitem(app.dependency_overrides, get_db, track_http_connection)
        other = workers.submit(hold_transaction)
        try:
            other_pid = pids.get(timeout=WAIT)
            waiting = workers.submit(send_review)
            review_pid = waiters.get(timeout=WAIT)
            assert other_pid != review_pid
            deadline = monotonic() + WAIT
            with SessionLocal() as db:
                while other_pid not in db.scalar(select(func.pg_blocking_pids(review_pid))):
                    assert not waiting.done() and monotonic() < deadline
                    sleep(0.01)
            while_waiting()
        finally:
            release.set()
        result = waiting.result(timeout=WAIT)
        other_result = other.result(timeout=WAIT)

    with SessionLocal() as db:
        assert not db.execute(text(
            "SELECT pid FROM pg_stat_activity WHERE pid = ANY(:pids) "
            "AND pid <> pg_backend_pid() AND xact_start IS NOT NULL"
        ), {"pids": [other_pid, review_pid]}).all()
    return result, other_result


def test_review_waiting_on_previous_publication_rechecks_the_expiration_boundary(client, scenario, monkeypatch, clock):
    with SessionLocal.begin() as db:
        previous, replacement = add_private_activity(db, scenario, ActivityRevisionStatus.APPROVED, ActivityRevisionStatus.PENDING)
    created = start(client, scenario)
    session_id = UUID(created.json()["impersonation_session_id"])
    deadline = datetime.fromisoformat(created.json()["expires_at"])
    clock.now = deadline - timedelta(microseconds=1)

    def lock_previous(db):
        db.get(ActivityRevision, previous.revision_id, with_for_update=True)
        db.commit()

    def reach_deadline():
        clock.now = deadline

    denied, _ = review_waiting_on_transaction(
        monkeypatch, lock_previous, lambda c: review(c, scenario, replacement, session_id), reach_deadline,
    )
    assert denied.status_code == 403, denied.text
    assert_unreviewed(replacement)
    with SessionLocal() as db:
        assert db.get(ActivityRevision, previous.revision_id).is_published
    invalidation, = [row for row in audits(session_id) if row.action == INVALIDATED]
    assert invalidation.occurred_at == deadline and invalidation.details == {"reason": "IMPERSONATION_TIMEOUT"}


def test_review_admitted_before_expiration_can_commit_while_end_waits(client, scenario, pending, monkeypatch, clock):
    created = start(client, scenario)
    session_id = UUID(created.json()["impersonation_session_id"])
    deadline = datetime.fromisoformat(created.json()["expires_at"])
    clock.now = admitted_at = deadline - timedelta(microseconds=1)

    def reach_deadline():
        clock.now = deadline

    accepted, ended = ordered_requests(
        monkeypatch,
        lambda c: review(c, scenario, pending, session_id),
        lambda c: end(c, scenario, session_id),
        while_waiting=reach_deadline,
    )
    assert accepted.status_code == 201, accepted.text
    assert ended.status_code == 200 and ended.json()["status"] == "EXPIRED"
    record, = review_records(pending)[0]
    assert record.reviewed_at == admitted_at
    assert [row.action for row in audits(session_id)] == [STARTED, REVIEWED, INVALIDATED]


def test_closure_worker_and_impersonated_review_cannot_both_resolve_revision(
    client, scenario, pending, represented_teacher, monkeypatch,
):
    assert scenario.designate(client, actor="admin_tab").status_code == 200
    request = scenario.request(client)
    assert request.status_code == 201, request.text
    set_request_deadline(request.json()["id"])

    denied, finalized = review_waiting_on_transaction(
        monkeypatch,
        lambda db: finalize_expired_closure_window(db, scenario.schools["own"], scenario.years["active"]),
        lambda c: review(c, scenario, pending, represented_teacher),
        lambda: None,
    )
    assert finalized == 1 and denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "ACADEMIC_YEAR_COMPLETION_WINDOW_ENDED"
    assert review_records(pending) == ([], [])
    with SessionLocal() as db:
        assert db.get(ActivityRevision, pending.revision_id).status == ActivityRevisionStatus.REJECTED
        assert db.get(ActivityDeadlineRejection, pending.revision_id) is not None
