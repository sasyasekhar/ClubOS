from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config.database import SessionLocal
from app.models.academic_year import AcademicYear
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.auth_session import AuthSession
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.enums import AcademicYearStatus, AccountType, ClubStatus
from app.models.representative_application import RepresentativeApplication
from app.schemas.representative_application import RepresentativeApplicationReviewRequest
from app.services.academic_year_closure_workflow import confirm_year_closure
from app.services.club_registration import ClubRequestError
from app.services.representative_applications import (
    apply_for_representative_position,
    review_representative_application,
)
from app.services.sessions import IDLE_TIMEOUT, token_hash
from test_academic_year_closure_workflow import (
    START,
    client,
    closure_audits,
    concurrent_requests,
    designated,
    ordered_transactions,
    scenario,
    set_request_deadline,
    year_status,
)
from test_academic_year_rollover import roll, sources


POSITIONS = "/clubs/available-representative-positions"
MY_APPLICATIONS = "/clubs/my-representative-applications"
APPLICATION_SUBMITTED = "CLUB_REPRESENTATIVE_APPLICATION_SUBMITTED"
APPLICATION_REVIEWED = "CLUB_REPRESENTATIVE_APPLICATION_REVIEWED"


@pytest.fixture
def applicants(scenario):
    with SessionLocal.begin() as db:
        for name, school, is_active in (
            ("second_student", "own", True),
            ("other_student", "other", True),
            ("inactive_student", "own", False),
        ):
            account_id = uuid4()
            db.add(Account(
                id=account_id, school_id=scenario.schools[school], account_type=AccountType.STUDENT,
                full_name=name, username=account_id.hex, email=f"{account_id.hex}@example.test",
                password_hash="fixture-hash", is_active=is_active,
            ))
            scenario.accounts[name] = account_id
        db.flush()
        for actor, account in (
            ("student_tab", "student"), ("second_student", "second_student"), ("second_student_tab", "second_student"),
            ("other_student", "other_student"), ("inactive_student", "inactive_student"),
        ):
            credentials = (uuid4().hex, uuid4().hex)
            scenario.credentials[actor] = credentials
            db.add(AuthSession(
                account_id=scenario.accounts[account], token_hash=token_hash(*credentials),
                expires_at=datetime.now(timezone.utc) + IDLE_TIMEOUT,
            ))


@pytest.fixture
def positions(client, scenario, sources):
    own_names = ("closed", "archived", "unselected")
    own = roll(client, scenario, [sources[name] for name in own_names])
    foreign = roll(client, scenario, [sources["foreign"]], target="other", actor="other_admin")
    assert own.status_code == foreign.status_code == 201
    new_clubs = {UUID(item["source_club_id"]): UUID(item["club_id"]) for item in own.json()["items"]}
    result = {name: new_clubs[sources[name]] for name in own_names}
    result["foreign"] = UUID(foreign.json()["items"][0]["club_id"])
    try:
        yield result
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(RepresentativeApplication).where(
                RepresentativeApplication.school_id.in_(scenario.schools.values()),
            ))


def application_path(club_id):
    return f"/clubs/{club_id}/representative-applications"


def apply(client, scenario, club_id, *, actor="student", key=None):
    return client.post(application_path(club_id), headers={
        **scenario.auth(actor), "Idempotency-Key": str(key or uuid4()),
    })


def review_application(client, scenario, club_id, application_id, *, actor="admin", decision="SELECTED", comments=None,
                       key=None):
    return client.post(f"{application_path(club_id)}/{application_id}/review", headers={
        **scenario.auth(actor), "Idempotency-Key": str(key or uuid4()),
    }, json={"decision": decision, "comments": comments})


def application_id(client, scenario, club_id, *, actor="student"):
    response = apply(client, scenario, club_id, actor=actor)
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def walk_pages(client, path, headers):
    params = {"limit": 1}
    while True:
        response = client.get(path, headers=headers, params=params)
        assert response.status_code == 200, response.text
        assert response.headers["Cache-Control"] == "private, no-store"
        page = response.json()
        assert len(page["items"]) <= 1
        yield from page["items"]
        if page["next_cursor"] is None:
            break
        assert page["next_cursor"] != params.get("after")
        params["after"] = page["next_cursor"]


def test_positions_list_is_bounded_and_school_scoped(client, scenario, positions, applicants):
    own = list(walk_pages(client, POSITIONS, scenario.auth("student")))
    assert {item["club_id"] for item in own} == {str(positions[name]) for name in ("closed", "archived", "unselected")}
    assert len(own) == 3
    assert {item["academic_year_id"] for item in own} == {str(scenario.years["active"])}
    assert {item["name"] for item in own} == {"closed", "archived", "unselected"}
    foreign = list(walk_pages(client, POSITIONS, scenario.auth("other_student")))
    assert [item["club_id"] for item in foreign] == [str(positions["foreign"])]
    for params in ({"limit": 51}, {"limit": 0}, {"after": "invalid"}):
        assert client.get(POSITIONS, headers=scenario.auth("student"), params=params).status_code == 422


def test_students_apply_to_multiple_clubs_and_see_only_their_own_applications(client, scenario, positions, applicants):
    assert list(walk_pages(client, MY_APPLICATIONS, scenario.auth("student"))) == []
    first = application_id(client, scenario, positions["closed"])
    second = application_id(client, scenario, positions["archived"])
    classmate = application_id(client, scenario, positions["closed"], actor="second_student")
    foreign = application_id(client, scenario, positions["foreign"], actor="other_student")

    own = list(walk_pages(client, MY_APPLICATIONS, scenario.auth("student")))
    assert len(own) == 2
    assert {item["id"] for item in own} == {str(first), str(second)}
    assert {item["status"] for item in own} == {"PENDING"}
    assert {item["applicant_account_id"] for item in own} == {str(scenario.accounts["student"])}
    assert all("reviewed_by_account_id" not in item for item in own)
    assert all(datetime.fromisoformat(item["submitted_at"]).tzinfo is not None for item in own)
    assert [item["id"] for item in walk_pages(client, MY_APPLICATIONS, scenario.auth("second_student"))] == [str(classmate)]
    assert [item["id"] for item in walk_pages(client, MY_APPLICATIONS, scenario.auth("other_student"))] == [str(foreign)]

    # The source club's student representative still had to apply, and the application creates no membership.
    with SessionLocal() as db:
        assert db.get(Club, positions["closed"]).created_by is None
        assert db.get(Club, positions["closed"]).status == ClubStatus.PENDING
        assert db.scalar(select(ClubMembership.id).where(ClubMembership.club_id == positions["closed"])) is None
    draft = client.post(f"/clubs/{positions['closed']}/activities", headers={
        **scenario.auth("student"), "Idempotency-Key": str(uuid4()),
    }, json={"title": "Workshop", "description": "Test workshop", "category": "STEM", "activity_date": str(START)})
    assert draft.status_code == 409


@pytest.mark.parametrize("actor,expected", [
    (None, 401), ("inactive_student", 401), ("teacher", 403), ("admin", 403), ("owner", 403),
])
def test_student_endpoints_require_an_active_student(client, scenario, positions, applicants, actor, expected):
    assert apply(client, scenario, positions["closed"], actor=actor).status_code == expected
    assert client.get(POSITIONS, headers=scenario.auth(actor)).status_code == expected
    assert client.get(MY_APPLICATIONS, headers=scenario.auth(actor)).status_code == expected
    assert not closure_audits(scenario, APPLICATION_SUBMITTED)


def test_application_rejects_foreign_and_ordinary_clubs_and_requires_key(client, scenario, positions, sources):
    unknown = apply(client, scenario, uuid4())
    foreign = apply(client, scenario, positions["foreign"])
    assert unknown.status_code == foreign.status_code == 404
    assert unknown.json() == foreign.json()
    assert apply(client, scenario, sources["current"]).status_code == 404
    assert client.post(application_path(positions["closed"]), headers=scenario.auth("student")).status_code == 422
    assert not closure_audits(scenario, APPLICATION_SUBMITTED)


def test_assigned_admin_lists_and_selects_applicant_without_resolving_other_applications(client, scenario, positions,
                                                                                         applicants):
    club_id = positions["closed"]
    first = application_id(client, scenario, club_id)
    competing = application_id(client, scenario, club_id, actor="second_student")
    admin_headers = scenario.auth("admin")
    seen = list(walk_pages(client, application_path(club_id), admin_headers))
    assert {item["id"] for item in seen} == {str(first), str(competing)}
    assert {item["applicant_name"] for item in seen} == {"student", "second_student"}
    assert all({"email", "username", "password_hash", "token_hash"}.isdisjoint(item) for item in seen)
    assert client.get(application_path(club_id), headers=admin_headers, params={"limit": 51}).status_code == 422

    key = uuid4()
    chosen = review_application(client, scenario, club_id, first, key=key, comments="Ready for the role.")
    assert chosen.status_code == 200, chosen.text
    assert chosen.json()["status"] == "SELECTED"
    assert chosen.json()["reviewed_by_account_id"] == str(scenario.accounts["admin"])
    assert chosen.json()["review_comments"] == "Ready for the role."
    assert datetime.fromisoformat(chosen.json()["reviewed_at"]).tzinfo is not None
    replay = review_application(client, scenario, club_id, first, actor="admin_tab", key=key, comments="Ready for the role.")
    assert replay.status_code == 200 and replay.json() == chosen.json()
    assert review_application(client, scenario, club_id, first, key=key, comments="Different decision note.").status_code == 409
    assert review_application(client, scenario, club_id, competing).status_code == 409

    selected, = closure_audits(scenario, APPLICATION_REVIEWED)
    assert selected.actor_account_id == scenario.accounts["admin"]
    assert selected.entity_id == str(first)
    assert selected.entity_type == "club_representative_application"
    remaining = list(walk_pages(client, MY_APPLICATIONS, scenario.auth("second_student")))
    assert [(item["id"], item["status"]) for item in remaining] == [(str(competing), "PENDING")]
    assert str(club_id) not in {item["club_id"] for item in walk_pages(client, POSITIONS, scenario.auth("second_student"))}
    assert apply(client, scenario, club_id).status_code == 409
    with SessionLocal() as db:
        assert db.get(Club, club_id).status == ClubStatus.PENDING
        assert db.scalar(select(ClubMembership.id).where(ClubMembership.club_id == club_id)) is None


@pytest.mark.parametrize("decision", ["REJECTED", "CHANGES_REQUESTED"])
def test_application_review_requires_and_preserves_an_explanation(client, scenario, positions, decision):
    club_id = positions["closed"]
    current = application_id(client, scenario, club_id)
    for comments in (None, "   "):
        result = review_application(client, scenario, club_id, current, decision=decision, comments=comments)
        assert result.status_code == 422
    explanation = "Please clarify availability for club meetings."
    result = review_application(client, scenario, club_id, current, decision=decision, comments=explanation)
    assert result.status_code == 200, result.text
    known, = list(walk_pages(client, MY_APPLICATIONS, scenario.auth("student")))
    assert (known["status"], known["review_comments"]) == (decision, explanation)
    assert len(closure_audits(scenario, APPLICATION_REVIEWED)) == 1


@pytest.mark.parametrize("actor,expected", [
    (None, 401), ("student", 403), ("teacher", 403), ("owner", 403), ("unassigned", 403), ("other_admin", 404),
])
def test_only_assigned_same_school_admin_can_list_or_review_applications(client, scenario, positions, actor, expected):
    club_id = positions["closed"]
    current = application_id(client, scenario, club_id)
    assert client.get(application_path(club_id), headers=scenario.auth(actor)).status_code == expected
    assert review_application(client, scenario, club_id, current, actor=actor).status_code == expected
    assert not closure_audits(scenario, APPLICATION_REVIEWED)


def test_admin_selection_revalidates_applicant_eligibility_and_application_scope(client, scenario, positions, applicants):
    club_id = positions["closed"]
    inactive = application_id(client, scenario, club_id, actor="second_student")
    elsewhere = application_id(client, scenario, positions["archived"])
    foreign = application_id(client, scenario, positions["foreign"], actor="other_student")
    with SessionLocal.begin() as db:
        db.get(Account, scenario.accounts["second_student"]).is_active = False

    assert review_application(client, scenario, club_id, inactive).status_code == 409
    for invalid_id in (elsewhere, foreign, scenario.accounts["student"], uuid4()):
        assert review_application(client, scenario, club_id, invalid_id).status_code == 404
    assert not closure_audits(scenario, APPLICATION_REVIEWED)


def test_application_and_review_replays_survive_audit_retention_and_year_closure(client, scenario, positions):
    club_id = positions["closed"]
    application_key, review_key = uuid4(), uuid4()
    first = apply(client, scenario, club_id, key=application_key)
    assert first.status_code == 201
    current = first.json()["id"]
    selected = review_application(client, scenario, club_id, current, key=review_key)
    assert selected.status_code == 200

    with SessionLocal.begin() as db:
        db.get(AcademicYear, scenario.years["active"]).status = AcademicYearStatus.CLOSED
        db.execute(delete(AuditLog).where(
            AuditLog.entity_id == current, AuditLog.action.in_((APPLICATION_SUBMITTED, APPLICATION_REVIEWED)),
        ))

    retry = apply(client, scenario, club_id, key=application_key)
    assert retry.status_code == 201
    assert (retry.json()["id"], retry.json()["status"]) == (current, "SELECTED")
    assert review_application(client, scenario, club_id, current, key=review_key).json() == selected.json()
    assert not closure_audits(scenario, APPLICATION_SUBMITTED)
    assert not closure_audits(scenario, APPLICATION_REVIEWED)


@pytest.mark.parametrize("condition", ["closed", "archived", "expired"])
def test_application_and_review_respect_historical_years_and_the_completion_deadline(client, scenario, positions,
                                                                                   applicants, designated, condition):
    club_id = positions["closed"]
    current = application_id(client, scenario, club_id)
    year_id = scenario.years["active"]
    if condition == "expired":
        requested = scenario.request(client)
        assert requested.status_code == 201
        set_request_deadline(requested.json()["id"])
    else:
        with SessionLocal.begin() as db:
            db.get(AcademicYear, year_id).status = AcademicYearStatus.CLOSED
        if condition == "archived":
            admin = scenario.auth("admin")
            assert client.post(f"/academic-years/{year_id}/export", headers=admin).status_code == 200
            archived = client.post(f"/academic-years/{year_id}/archive", headers={
                **admin, "Idempotency-Key": str(uuid4()),
            }, json={"export_saved": True})
            assert archived.status_code == 200, archived.text

    assert client.get(POSITIONS, headers=scenario.auth("second_student")).json()["items"] == []
    new = apply(client, scenario, club_id, actor="second_student")
    decision = review_application(client, scenario, club_id, current)
    assert new.status_code == decision.status_code == 409
    if condition == "expired":
        assert new.json()["detail"]["code"] == "ACADEMIC_YEAR_COMPLETION_WINDOW_ENDED"
        assert decision.json()["detail"]["code"] == "ACADEMIC_YEAR_COMPLETION_WINDOW_ENDED"
    own, = list(walk_pages(client, MY_APPLICATIONS, scenario.auth("student")))
    assert (own["id"], own["status"]) == (str(current), "PENDING")
    assert len(list(walk_pages(client, application_path(club_id), scenario.auth("admin")))) == 1
    assert len(closure_audits(scenario, APPLICATION_SUBMITTED)) == 1
    assert not closure_audits(scenario, APPLICATION_REVIEWED)


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_duplicate_applications_create_one_application_and_audit(scenario, positions, applicants, monkeypatch,
                                                                         same_key):
    club_id = positions["closed"]
    key = uuid4()
    first, second = concurrent_requests(
        monkeypatch,
        lambda client: apply(client, scenario, club_id, key=key),
        lambda client: apply(client, scenario, club_id, actor="student_tab", key=key if same_key else uuid4()),
    )
    assert sorted([first.status_code, second.status_code]) == ([201, 201] if same_key else [201, 409])
    if same_key:
        assert first.json() == second.json()
    submitted, = closure_audits(scenario, APPLICATION_SUBMITTED)
    assert submitted.actor_account_id == scenario.accounts["student"]


@pytest.mark.parametrize("competing", [True, False])
def test_concurrent_application_selections_create_one_decision_and_audit(client, scenario, positions, applicants,
                                                                         monkeypatch, competing):
    club_id = positions["closed"]
    first_id = application_id(client, scenario, club_id)
    second_id = application_id(client, scenario, club_id, actor="second_student") if competing else first_id
    key = uuid4()
    first, second = concurrent_requests(
        monkeypatch,
        lambda client: review_application(client, scenario, club_id, first_id, key=key),
        lambda client: review_application(
            client, scenario, club_id, second_id, actor="admin_tab", key=uuid4() if competing else key,
        ),
    )
    assert sorted([first.status_code, second.status_code]) == ([200, 409] if competing else [200, 200])
    rows = list(walk_pages(client, application_path(club_id), scenario.auth("admin")))
    selected, = [item for item in rows if item["status"] == "SELECTED"]
    reviewed, = closure_audits(scenario, APPLICATION_REVIEWED)
    assert reviewed.entity_id == selected["id"]
    if not competing:
        assert first.json() == second.json()
    with SessionLocal() as db:
        assert db.scalar(select(ClubMembership.id).where(ClubMembership.club_id == club_id)) is None


@pytest.mark.parametrize("operation", ["apply", "review"])
def test_failed_application_writes_rollback_the_decision_and_audit(client, scenario, positions, operation):
    club_id = positions["closed"]
    key = uuid4()
    if operation == "apply":
        action = APPLICATION_SUBMITTED
        submit = lambda: apply(client, scenario, club_id, key=key)
        expected = 201
    else:
        current = application_id(client, scenario, club_id)
        action = APPLICATION_REVIEWED
        submit = lambda: review_application(client, scenario, club_id, current, key=key)
        expected = 200

    def fail_after_flush(db, _context):
        if any(isinstance(row, AuditLog) and row.action == action for row in db.new):
            db.execute(text("SELECT 1 / 0"))

    event.listen(Session, "after_flush", fail_after_flush)
    try:
        assert submit().status_code == 503
    finally:
        event.remove(Session, "after_flush", fail_after_flush)

    assert not closure_audits(scenario, action)
    rows = list(walk_pages(client, MY_APPLICATIONS, scenario.auth("student")))
    assert [item["status"] for item in rows] == ([] if operation == "apply" else ["PENDING"])
    assert submit().status_code == expected
    assert len(closure_audits(scenario, action)) == 1


@pytest.mark.parametrize("operation", ["apply", "review"])
@pytest.mark.parametrize("closure_first", [False, True])
def test_application_writes_race_with_actual_closure(client, scenario, positions, designated, operation, closure_first):
    club_id = positions["closed"]
    year_id = scenario.years["active"]
    requested = scenario.request(client)
    assert requested.status_code == 201
    request_id = UUID(requested.json()["id"])

    if operation == "review":
        current = application_id(client, scenario, club_id)

    def mutate(db):
        if operation == "apply":
            student = db.get(Account, scenario.accounts["student"])
            return apply_for_representative_position(db, student, club_id, uuid4())

        admin = db.get(Account, scenario.accounts["admin"])
        data = RepresentativeApplicationReviewRequest(decision="SELECTED")
        return review_representative_application(db, admin, club_id, current, data, uuid4())

    def close(db):
        admin = db.get(Account, scenario.accounts["admin"])
        return confirm_year_closure(db, admin, year_id, request_id)

    action = APPLICATION_SUBMITTED if operation == "apply" else APPLICATION_REVIEWED
    if closure_first:
        set_request_deadline(request_id)
        rejected = ordered_transactions(close, mutate, ClubRequestError)
        assert rejected.status_code == 409
        assert not closure_audits(scenario, action)
    else:
        confirmed = ordered_transactions(mutate, close, while_first_waits=lambda: set_request_deadline(request_id))
        assert confirmed.status.value == "CONFIRMED"
        assert len(closure_audits(scenario, action)) == 1

    assert year_status(scenario) == AcademicYearStatus.CLOSED
    entries = list(walk_pages(client, MY_APPLICATIONS, scenario.auth("student")))
    if operation == "review":
        assert [(item["id"], item["status"]) for item in entries] == [(
            str(current), "PENDING" if closure_first else "SELECTED",
        )]
    else:
        assert [item["status"] for item in entries] == ([] if closure_first else ["PENDING"])


def test_database_rejects_invalid_application_relationships_and_states(client, scenario, positions, applicants):
    club_id = positions["closed"]
    current = application_id(client, scenario, club_id)
    pending = application_id(client, scenario, club_id, actor="second_student")
    assert review_application(client, scenario, club_id, current).status_code == 200

    def insert_application(db, *, school="own", club=club_id, applicant="student"):
        db.add(RepresentativeApplication(
            id=uuid4(), school_id=scenario.schools[school], club_id=club,
            applicant_account_id=scenario.accounts[applicant],
        ))

    def review_directly(db, *, reviewer="admin", status="SELECTED", comments=None):
        db.execute(update(RepresentativeApplication).where(RepresentativeApplication.id == pending).values(
            status=status, reviewed_by_account_id=scenario.accounts[reviewer],
            reviewed_at=datetime.now(timezone.utc), review_operation_id=uuid4(), review_comments=comments,
        ))

    for constraint, operation in (
        ("uq_representative_application_club_applicant", insert_application),
        ("fk_representative_application_club_school", lambda db: insert_application(db, club=positions["foreign"])),
        ("fk_representative_application_applicant_school", lambda db: insert_application(db, applicant="other_student")),
        ("fk_representative_application_reviewer_school", lambda db: review_directly(
            db, reviewer="other_admin", status="REJECTED", comments="Out of scope",
        )),
        ("uq_one_selected_representative_application_per_club", review_directly),
        ("ck_representative_application_review", lambda db: review_directly(db, status="REJECTED")),
        ("ck_representative_application_status", lambda db: review_directly(db, status="UNKNOWN", comments="Invalid")),
    ):
        with pytest.raises(IntegrityError) as exc_info, SessionLocal.begin() as db:
            operation(db)
        assert exc_info.value.orig.diag.constraint_name == constraint
