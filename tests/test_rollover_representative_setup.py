import csv
from io import BytesIO, StringIO
from uuid import uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config.database import SessionLocal
from app.models.account import Account
from app.models.activity import Activity
from app.models.audit_log import AuditLog
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.club_review import ClubReview
from app.models.enums import ClubRole, ClubStatus, MembershipStatus
from test_academic_year_closure_workflow import (
    START, client, closure_audits, concurrent_requests, designated, scenario, set_request_deadline,
)
from test_academic_year_rollover import sources
from test_representative_applications import applicants, application_id, positions, review_application, walk_pages


def headers(scenario, actor, key=None):
    return {**scenario.auth(actor), "Idempotency-Key": str(key or uuid4())}


def select_teacher(client, scenario, club_id, *, actor="second_student", teacher="replacement", key=None, path="select-teacher"):
    return client.post(f"/clubs/{club_id}/{path}", headers=headers(scenario, actor, key),
                       json={"teacher_account_id": str(scenario.accounts[teacher])})


def answer_invitation(client, scenario, club_id, teacher="replacement", decision="ACCEPT", *, membership_id=None):
    auth = scenario.auth(teacher)
    if membership_id is None:
        pending = client.get("/clubs/teacher-invitations", headers=auth)
        assert pending.status_code == 200, pending.text
        membership_id = next((row["membership_id"] for row in pending.json() if row["club_id"] == str(club_id)), None)
    payload = {"decision": decision}
    if membership_id is not None:
        payload["membership_id"] = membership_id
    return client.post(f"/clubs/{club_id}/teacher-invitation", headers=auth, json=payload)


def approve(client, scenario, club_id, *, actor="admin", key=None):
    return client.post(f"/clubs/{club_id}/reviews", headers=headers(scenario, actor, key), json={"decision": "APPROVED"})


def create_activity(client, scenario, club_id, actor):
    return client.post(f"/clubs/{club_id}/activities", headers=headers(scenario, actor), json={
        "title": "New workshop", "description": "Current-year report", "activity_date": START.isoformat(), "category": "STEM",
    })


def memberships(club_id):
    with SessionLocal() as db:
        return list(db.scalars(select(ClubMembership).where(ClubMembership.club_id == club_id)))


@pytest.fixture
def rollover_club(positions, applicants):
    club_id = positions["closed"]
    try:
        yield club_id
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(ClubReview).where(ClubReview.club_id == club_id))


@pytest.fixture
def selected_club(client, scenario, rollover_club):
    application = application_id(client, scenario, rollover_club, actor="second_student")
    selected = review_application(client, scenario, rollover_club, application)
    assert selected.status_code == 200, selected.text
    return rollover_club


def test_activation_uses_the_selected_applicant_and_preserves_original_history(client, scenario, sources, rollover_club):
    club_id = rollover_club
    assert memberships(club_id) == []
    assert approve(client, scenario, club_id).status_code == 409
    application = application_id(client, scenario, club_id, actor="second_student")
    assert approve(client, scenario, club_id).status_code == 409
    assert review_application(client, scenario, club_id, application).status_code == 200
    assert memberships(club_id) == []
    assert approve(client, scenario, club_id).status_code == 409
    assert create_activity(client, scenario, club_id, "second_student").status_code == 409

    invite = select_teacher(client, scenario, club_id)
    assert invite.status_code == 201, invite.text
    assert invite.json()["status"] == "PENDING"
    assert approve(client, scenario, club_id).status_code == 409
    assert answer_invitation(client, scenario, club_id, "teacher").status_code == 404
    assert answer_invitation(client, scenario, club_id).json()["status"] == "ACTIVE"
    assert create_activity(client, scenario, club_id, "second_student").status_code == 409

    key = uuid4()
    approval = approve(client, scenario, club_id, key=key)
    assert approval.status_code == 201, approval.text
    assert approval.json()["club_status"] == "ACTIVE"
    assert approve(client, scenario, club_id, key=key).json() == approval.json()
    assert {row.actor_account_id for row in closure_audits(scenario, "CLUB_REVIEWED")} == {scenario.accounts["admin"]}
    assert len(closure_audits(scenario, "CLUB_REVIEWED")) == 1
    assert {
        (row.role, row.account_id, row.status) for row in memberships(club_id)
    } == {
        (ClubRole.STUDENT_REP, scenario.accounts["second_student"], MembershipStatus.ACTIVE),
        (ClubRole.TEACHER_REP, scenario.accounts["replacement"], MembershipStatus.ACTIVE),
    }
    assert {row.account_id for row in memberships(sources["closed"])} == {
        scenario.accounts["student"], scenario.accounts["teacher"],
    }
    with SessionLocal() as db:
        original = db.get(Club, sources["closed"])
        carried = db.get(Club, club_id)
        assert carried.created_by is None and carried.rolled_over_from_club_id == original.id
        assert original.created_by == scenario.accounts["student"]
        assert db.scalar(select(Activity.id).where(Activity.club_id == original.id)) is not None
        assert db.scalar(select(Activity.id).where(Activity.club_id == carried.id)) is None

    activity = create_activity(client, scenario, club_id, "second_student")
    assert activity.status_code == 201, activity.text
    assert create_activity(client, scenario, club_id, "student").status_code == 403
    activity_id, revision_id = (activity.json()[field] for field in ("activity_id", "revision_id"))
    base = f"/clubs/{club_id}/activities/{activity_id}/revisions/{revision_id}"
    submit = client.post(f"{base}/submit", headers=headers(scenario, "second_student"))
    assert submit.status_code == 200, submit.text
    assert client.post(f"{base}/review", headers=headers(scenario, "teacher"), json={"decision": "APPROVED"}).status_code == 403
    teacher_review = client.post(f"{base}/review", headers=headers(scenario, "replacement"), json={"decision": "APPROVED"})
    assert teacher_review.status_code == 201, teacher_review.text

    export = client.post(f"/academic-years/{scenario.years['active']}/export", headers=scenario.auth("admin"))
    assert export.status_code == 200, export.text
    with ZipFile(BytesIO(export.content)) as archive:
        rows = csv.DictReader(StringIO(archive.read("representative_memberships.csv").decode("utf-8")))
        assert {(row["representative_account_id"], row["role"]) for row in rows if row["club_id"] == str(club_id)} == {
            (str(scenario.accounts["second_student"]), "STUDENT_REP"), (str(scenario.accounts["replacement"]), "TEACHER_REP"),
        }
        assert not any("application" in name for name in archive.namelist())


def test_only_selected_student_can_invite_an_eligible_teacher(client, scenario, selected_club):
    assert select_teacher(client, scenario, selected_club, actor="student").status_code in (403, 404)
    assert select_teacher(client, scenario, selected_club, teacher="other_teacher").status_code == 404
    assert select_teacher(client, scenario, selected_club, teacher="inactive").status_code == 404
    assert select_teacher(client, scenario, selected_club, teacher="student").status_code == 404
    assert answer_invitation(client, scenario, selected_club, "teacher").status_code == 404
    assert memberships(selected_club) == []


def test_database_rejects_a_cross_school_rollover_membership(scenario, selected_club):
    with pytest.raises(IntegrityError) as error, SessionLocal.begin() as db:
        db.add(ClubMembership(
            club_id=selected_club, account_id=scenario.accounts["other_teacher"],
            role=ClubRole.TEACHER_REP, status=MembershipStatus.PENDING, start_date=START,
        ))
    assert error.value.orig.diag.constraint_name == "ck_rollover_membership_same_school"


def test_eligible_teachers_are_listed_only_for_the_selected_student(client, scenario, selected_club):
    path = f"/clubs/{selected_club}/eligible-teachers"
    student_headers = scenario.auth("second_student")
    assert client.get(path, headers=student_headers, params={"limit": 51}).status_code == 422
    teachers = list(walk_pages(client, path, student_headers))
    assert {row["teacher_account_id"] for row in teachers} == {
        str(scenario.accounts["teacher"]), str(scenario.accounts["replacement"]),
    }
    assert all(set(row) == {"teacher_account_id", "full_name"} for row in teachers)
    assert client.get(path, headers=scenario.auth("student")).status_code in (403, 404)
    assert client.get(path, headers=scenario.auth("other_student")).status_code == 404
    assert client.get(path, headers=scenario.auth("teacher")).status_code == 403


def test_teacher_can_decline_then_selected_student_can_replace_them(client, scenario, selected_club):
    key = uuid4()
    first = select_teacher(client, scenario, selected_club, teacher="teacher", key=key)
    assert first.status_code == 201, first.text
    assert select_teacher(client, scenario, selected_club, teacher="teacher", key=key).json() == first.json()
    assert select_teacher(client, scenario, selected_club, key=key).status_code == 409
    assert select_teacher(client, scenario, selected_club, teacher="teacher").status_code == 409
    membership_id = first.json()["membership_id"]
    for _ in range(2):
        decline = answer_invitation(client, scenario, selected_club, "teacher", "DECLINE", membership_id=membership_id)
        assert decline.json()["status"] == "DECLINED"
    assert select_teacher(client, scenario, selected_club).status_code == 409
    replaced = select_teacher(client, scenario, selected_club, path="replace-teacher")
    assert replaced.status_code == 201, replaced.text
    assert answer_invitation(client, scenario, selected_club).json()["status"] == "ACTIVE"
    assert approve(client, scenario, selected_club).status_code == 201
    assert {row.status for row in memberships(selected_club)} == {MembershipStatus.ACTIVE, MembershipStatus.DECLINED}
    assert len(closure_audits(scenario, "TEACHER_INVITATION_DECLINED")) == 1
    assert len(closure_audits(scenario, "TEACHER_REPLACEMENT_REQUESTED")) == 1


def test_delayed_reply_cannot_resolve_a_later_invitation_to_the_same_teacher(client, scenario, selected_club):
    first = select_teacher(client, scenario, selected_club, teacher="teacher")
    assert first.status_code == 201, first.text
    first_id = first.json()["membership_id"]
    assert answer_invitation(client, scenario, selected_club, "teacher", "DECLINE", membership_id=first_id).status_code == 200
    second = select_teacher(client, scenario, selected_club, teacher="teacher", path="replace-teacher")
    assert second.status_code == 201, second.text
    second_id = second.json()["membership_id"]
    assert first_id != second_id

    retry = answer_invitation(client, scenario, selected_club, "teacher", "DECLINE", membership_id=first_id)
    assert retry.json()["membership_id"] == first_id
    assert answer_invitation(client, scenario, selected_club, "teacher", membership_id=first_id).status_code == 409
    missing_id = client.post(f"/clubs/{selected_club}/teacher-invitation", headers=scenario.auth("teacher"),
                             json={"decision": "ACCEPT"})
    assert missing_id.status_code in (409, 422)
    assert approve(client, scenario, selected_club).status_code == 409
    accepted = answer_invitation(client, scenario, selected_club, "teacher", membership_id=second_id)
    assert accepted.json()["membership_id"] == second_id
    assert approve(client, scenario, selected_club).status_code == 201


@pytest.mark.parametrize("actor", ["second_student", "replacement"])
def test_approval_revalidates_both_representatives(client, scenario, selected_club, actor):
    assert select_teacher(client, scenario, selected_club).status_code == 201
    assert answer_invitation(client, scenario, selected_club).status_code == 200

    with SessionLocal.begin() as db:
        db.execute(update(Account).where(Account.id == scenario.accounts[actor]).values(is_active=False))
    key = uuid4()
    assert approve(client, scenario, selected_club, key=key).status_code == 409
    assert not closure_audits(scenario, "CLUB_REVIEWED")

    with SessionLocal.begin() as db:
        db.execute(update(Account).where(Account.id == scenario.accounts[actor]).values(is_active=True))
    assert approve(client, scenario, selected_club, key=key).status_code == 201


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_teacher_invitations_create_one_membership(scenario, selected_club, monkeypatch, same_key):
    key = uuid4()
    first, second = concurrent_requests(
        monkeypatch,
        lambda client: select_teacher(client, scenario, selected_club, key=key),
        lambda client: select_teacher(client, scenario, selected_club, actor="second_student_tab", key=key if same_key else uuid4()),
    )
    assert sorted((first.status_code, second.status_code)) == ([201, 201] if same_key else [201, 409])
    if same_key:
        assert first.json() == second.json()
    assert len(memberships(selected_club)) == 1
    assert len(closure_audits(scenario, "TEACHER_INVITATION_REQUESTED")) == 1


def test_concurrent_approvals_create_one_student_membership(client, scenario, selected_club, monkeypatch):
    assert select_teacher(client, scenario, selected_club).status_code == 201
    assert answer_invitation(client, scenario, selected_club).status_code == 200
    key = uuid4()
    first, second = concurrent_requests(
        monkeypatch,
        lambda client: approve(client, scenario, selected_club, key=key),
        lambda client: approve(client, scenario, selected_club, actor="admin_tab", key=key),
    )
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert len([row for row in memberships(selected_club) if row.role == ClubRole.STUDENT_REP]) == 1
    assert len(closure_audits(scenario, "CLUB_REVIEWED")) == 1


def test_failed_approval_rolls_back_activation_membership_and_audit(client, scenario, selected_club):
    assert select_teacher(client, scenario, selected_club).status_code == 201
    assert answer_invitation(client, scenario, selected_club).status_code == 200

    def abort_after_flush(db, _context):
        if any(isinstance(row, AuditLog) and row.action == "CLUB_REVIEWED" for row in db.new):
            db.execute(text("SELECT 1 / 0"))

    key = uuid4()
    event.listen(Session, "after_flush", abort_after_flush)
    try:
        rejected = approve(client, scenario, selected_club, key=key)
    finally:
        event.remove(Session, "after_flush", abort_after_flush)
    assert rejected.status_code == 503
    assert not closure_audits(scenario, "CLUB_REVIEWED")
    assert [row.role for row in memberships(selected_club)] == [ClubRole.TEACHER_REP]
    with SessionLocal() as db:
        assert db.get(Club, selected_club).status == ClubStatus.PENDING
    assert approve(client, scenario, selected_club, key=key).status_code == 201


@pytest.mark.parametrize("operation", ["invite", "accept", "replace", "approve"])
def test_setup_writes_stop_at_the_academic_year_deadline(client, scenario, selected_club, designated, operation):
    if operation != "invite":
        assert select_teacher(client, scenario, selected_club, teacher="teacher").status_code == 201
    if operation == "replace":
        assert answer_invitation(client, scenario, selected_club, "teacher", "DECLINE").status_code == 200
    if operation == "approve":
        assert answer_invitation(client, scenario, selected_club, "teacher").status_code == 200

    request = scenario.request(client)
    assert request.status_code == 201, request.text
    set_request_deadline(request.json()["id"])
    operations = {
        "invite": lambda: select_teacher(client, scenario, selected_club),
        "accept": lambda: answer_invitation(client, scenario, selected_club, "teacher"),
        "replace": lambda: select_teacher(client, scenario, selected_club, path="replace-teacher"),
        "approve": lambda: approve(client, scenario, selected_club),
    }
    rejected = operations[operation]()
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "ACADEMIC_YEAR_COMPLETION_WINDOW_ENDED"


def test_selected_student_can_rejoin_after_activation(client, scenario, selected_club):
    assert select_teacher(client, scenario, selected_club).status_code == 201
    assert answer_invitation(client, scenario, selected_club).status_code == 200
    assert approve(client, scenario, selected_club).status_code == 201
    student, = [row for row in memberships(selected_club) if row.role == ClubRole.STUDENT_REP]

    end = client.post(f"/clubs/{selected_club}/memberships/{student.id}/end", headers=headers(scenario, "admin"), json={})
    assert end.status_code == 200, end.text
    rejoin = client.post(f"/clubs/{selected_club}/memberships/{student.id}/rejoin", headers=headers(scenario, "admin"))
    assert rejoin.status_code == 201, rejoin.text
    assert rejoin.json()["account_id"] == str(scenario.accounts["second_student"])
    assert rejoin.json()["membership_id"] != str(student.id)
