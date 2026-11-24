from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config.database import SessionLocal
from app.models.academic_year import AcademicYear
from app.models.account import Account
from app.models.activity import Activity
from app.models.activity_attachment import ActivityAttachment
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.club_review import ClubReview
from app.models.enums import (
    AcademicYearStatus,
    ActivityRevisionStatus,
    ClubReviewDecision,
    ClubRole,
    ClubStatus,
    MembershipStatus,
)
from app.schemas.academic_year_rollover import ClubRolloverRequest
from app.services.academic_year_closure_workflow import confirm_year_closure
from app.services.academic_year_rollover import rollover_clubs
from app.services.academic_years import AcademicYearError
from test_academic_year_closure_workflow import (
    END,
    PATH,
    START,
    WAIT,
    client,
    closure_audits,
    concurrent_requests,
    designated,
    ordered_transactions,
    run_transaction,
    scenario,
    set_request_deadline,
)


ROLLED_OVER = "ACADEMIC_YEAR_CLUBS_ROLLED_OVER"


def roll(client, scenario, source_ids, *, target="active", actor="admin", key=None):
    return client.post(f"{PATH}/{scenario.years[target]}/rollover-clubs", headers={
        **scenario.auth(actor), "Idempotency-Key": str(key or uuid4()),
    }, json={"source_club_ids": [str(source_id) for source_id in source_ids]})


def candidates(client, scenario, *, target="active", actor="admin", **params):
    return client.get(
        f"{PATH}/{scenario.years[target]}/rollover-candidates", headers=scenario.auth(actor), params=params,
    )


@pytest.fixture
def sources(scenario):
    with SessionLocal.begin() as db:
        archived = AcademicYear(
            school_id=scenario.schools["own"], name="Archived year", status=AcademicYearStatus.ARCHIVED,
            start_date=START, end_date=END,
        )
        foreign_history = AcademicYear(
            school_id=scenario.schools["other"], name="Other year", status=AcademicYearStatus.CLOSED,
            start_date=START, end_date=END,
        )
        db.add_all([archived, foreign_history])
        db.flush()

        def historical_club(name, school_id, year_id, creator_id, status):
            return Club(
                school_id=school_id, academic_year_id=year_id, created_by=creator_id,
                name=name, description=f"Description for {name}", category="STEM", status=status,
                logo_key=f"test/old-private-logo-{uuid4()}",
            )

        rows = {
            name: historical_club(name, scenario.schools["own"], year_id, scenario.accounts["student"], status)
            for name, year_id, status in (
                ("closed", scenario.years["historical"], ClubStatus.ACTIVE),
                ("archived", archived.id, ClubStatus.ACTIVE),
                ("unselected", scenario.years["historical"], ClubStatus.ACTIVE),
                ("unapproved", scenario.years["historical"], ClubStatus.PENDING),
                ("rejected", scenario.years["historical"], ClubStatus.REJECTED),
                ("current", scenario.years["active"], ClubStatus.ACTIVE),
            )
        }
        rows["foreign"] = historical_club(
            "foreign", scenario.schools["other"], foreign_history.id, scenario.accounts["other_teacher"], ClubStatus.ACTIVE,
        )
        db.add_all(rows.values())
        db.flush()
        ids = {name: club.id for name, club in rows.items()}
        closed_id = ids["closed"]
        db.add_all([
            ClubMembership(
                club_id=closed_id, account_id=scenario.accounts[actor], role=role,
                start_date=START, status=MembershipStatus.ACTIVE,
            )
            for actor, role in (("student", ClubRole.STUDENT_REP), ("teacher", ClubRole.TEACHER_REP))
        ])
        db.add(ClubReview(
            club_id=closed_id, admin_account_id=scenario.accounts["admin"], decision=ClubReviewDecision.APPROVED,
        ))
        activity = Activity(club_id=closed_id, created_by=scenario.accounts["student"])
        db.add(activity)
        db.flush()
        revision = ActivityRevision(
            activity_id=activity.id, revision_number=1, status=ActivityRevisionStatus.APPROVED,
            title="Historical activity", activity_date=START, description="Historical description", category="STEM",
            is_published=True,
        )
        db.add(revision)
        db.flush()
        db.add(ActivityAttachment(
            revision_id=revision.id, file_name="historical.pdf", file_type="application/pdf", storage_key="test/history",
        ))

    try:
        yield ids
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(ClubReview).where(ClubReview.club_id == ids["closed"]))


def carried_clubs(scenario):
    with SessionLocal() as db:
        return list(db.scalars(select(Club).where(
            Club.school_id == scenario.schools["own"], Club.rolled_over_from_club_id.is_not(None),
        )))


def test_selective_rollover_copies_configuration_and_preserves_history(client, scenario, sources):
    def history():
        with SessionLocal() as db:
            activities = select(Activity.id).where(Activity.club_id == sources["closed"])
            revisions = select(ActivityRevision.id).where(ActivityRevision.activity_id.in_(activities))
            return [db.execute(query).all() for query in (
                select(Club.__table__).where(Club.id.in_(sources.values())).order_by(Club.id),
                select(ClubMembership.__table__).where(ClubMembership.club_id == sources["closed"]).order_by(ClubMembership.id),
                select(ClubReview.__table__).where(ClubReview.club_id == sources["closed"]),
                select(Activity.__table__).where(Activity.id.in_(activities)),
                select(ActivityRevision.__table__).where(ActivityRevision.id.in_(revisions)),
                select(ActivityAttachment.__table__).where(ActivityAttachment.revision_id.in_(revisions)),
            )]

    before = history()
    selection = [sources["closed"], sources["archived"]]
    response = roll(client, scenario, selection)
    assert response.status_code == 201, response.text
    assert response.json()["target_year_id"] == str(scenario.years["active"])
    items = response.json()["items"]
    assert {item["source_club_id"] for item in items} == {str(source) for source in selection}
    new_ids = {UUID(item["club_id"]) for item in items}
    assert new_ids.isdisjoint(sources.values()) and len(new_ids) == len(selection)
    assert history() == before

    with SessionLocal() as db:
        for item in items:
            original = db.get(Club, UUID(item["source_club_id"]))
            new = db.get(Club, UUID(item["club_id"]))
            assert new.rolled_over_from_club_id == original.id
            assert new.academic_year_id == scenario.years["active"]
            assert (new.name, new.description, new.category) == (original.name, original.description, original.category)
            assert new.created_by is None and new.logo_key is None
            assert new.status == ClubStatus.PENDING
        assert not db.scalars(select(Activity.id).where(Activity.club_id.in_(new_ids))).all()
        assert not db.scalars(select(ClubMembership.id).where(ClubMembership.club_id.in_(new_ids))).all()
        assert not db.scalars(select(ClubReview.id).where(ClubReview.club_id.in_(new_ids))).all()

    assert client.get("/activities/published", headers=scenario.auth("student")).json() == []
    assert len(closure_audits(scenario, ROLLED_OVER)) == 1


def test_candidate_listing_is_paginated_scoped_and_marks_carried_clubs(client, scenario, sources):
    response = roll(client, scenario, [sources["closed"]])
    assert response.status_code == 201
    seen = {}
    params = {"limit": 1}
    while True:
        page = candidates(client, scenario, **params)
        assert page.status_code == 200, page.text
        assert page.headers["Cache-Control"] == "private, no-store"
        assert len(page.json()["items"]) <= 1
        for item in page.json()["items"]:
            assert item["source_club_id"] not in seen
            seen[item["source_club_id"]] = item
        cursor = page.json()["next_cursor"]
        if cursor is None:
            break
        params["after"] = cursor

    assert set(seen) == {str(sources[name]) for name in ("closed", "archived", "unselected")}
    assert seen[str(sources["closed"])] == {
        "source_club_id": str(sources["closed"]),
        "name": "closed", "description": "Description for closed", "category": "STEM",
        "source_academic_year_id": str(scenario.years["historical"]), "source_academic_year_name": "historical",
        "previous_status": "ACTIVE", "already_carried_forward": True,
    }
    assert seen[str(sources["archived"])]["already_carried_forward"] is False
    assert candidates(client, scenario, limit=51).status_code == 422


@pytest.mark.parametrize("actor", ["student", "teacher", "owner", "unassigned"])
def test_rollover_requires_the_assigned_school_admin(client, scenario, sources, actor):
    assert candidates(client, scenario, actor=actor).status_code == 403
    assert roll(client, scenario, [sources["closed"]], actor=actor).status_code == 403
    assert not carried_clubs(scenario)


@pytest.mark.parametrize("invalid", ["current", "unapproved", "rejected", "foreign", "unknown"])
def test_invalid_batches_make_no_partial_changes(client, scenario, sources, invalid):
    invalid_id = sources.get(invalid, uuid4())
    result = roll(client, scenario, [sources["closed"], invalid_id])
    assert result.status_code == (404 if invalid in ("foreign", "unknown") else 409)
    assert not carried_clubs(scenario)
    assert not closure_audits(scenario, ROLLED_OVER)


def test_targets_are_scoped_and_must_be_active(client, scenario, sources):
    assert candidates(client, scenario, target="other").status_code == 404
    assert roll(client, scenario, [sources["closed"]], target="other").status_code == 404
    assert roll(client, scenario, [sources["closed"]], target="historical").status_code == 409
    assert not carried_clubs(scenario)


def test_rollover_honors_the_active_completion_deadline(client, scenario, designated, sources):
    request = scenario.request(client)
    assert request.status_code == 201
    set_request_deadline(request.json()["id"])
    result = roll(client, scenario, [sources["closed"]])
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "ACADEMIC_YEAR_COMPLETION_WINDOW_ENDED"
    assert not carried_clubs(scenario)


def test_read_only_replay_survives_year_closure_and_audit_retention(client, scenario, sources):
    key = uuid4()
    selection = [sources["closed"], sources["archived"]]
    result = roll(client, scenario, selection, key=key)
    assert result.status_code == 201
    assert roll(client, scenario, selection[::-1], key=key).json() == result.json()
    changed_selection = roll(client, scenario, [sources["closed"]], key=key)
    changed_target = roll(client, scenario, selection, key=key, target="historical")
    assert changed_selection.status_code == changed_target.status_code == 409
    assert "Idempotency key reused" in changed_selection.json()["detail"]
    assert roll(client, scenario, [sources["closed"], sources["unselected"]]).status_code == 409
    assert len(carried_clubs(scenario)) == 2
    assert len(closure_audits(scenario, ROLLED_OVER)) == 1

    with SessionLocal.begin() as db:
        db.get(AcademicYear, scenario.years["active"]).status = AcademicYearStatus.CLOSED
        db.execute(delete(AuditLog).where(
            AuditLog.action == ROLLED_OVER, AuditLog.entity_id == str(scenario.years["active"]),
        ))
    assert roll(client, scenario, selection, key=key).json() == result.json()
    assert roll(client, scenario, [sources["unselected"]]).status_code == 409
    assert not closure_audits(scenario, ROLLED_OVER)


def test_unassigned_rollover_cannot_be_approved_or_managed_by_the_old_representatives(client, scenario, sources):
    result = roll(client, scenario, [sources["closed"]])
    assert result.status_code == 201
    club_id = result.json()["items"][0]["club_id"]
    for decision, body in (
        ("APPROVED", {}), ("REJECTED", {"comments": "Test comments"}),
    ):
        review = client.post(f"/clubs/{club_id}/reviews", headers={
            **scenario.auth("admin"), "Idempotency-Key": str(uuid4()),
        }, json={"decision": decision, **body})
        assert review.status_code == 409

    draft = client.post(f"/clubs/{club_id}/activities", headers={
        **scenario.auth("student"), "Idempotency-Key": str(uuid4()),
    }, json={"title": "New draft", "activity_date": str(START), "description": "Description", "category": "STEM"})
    assert draft.status_code == 409
    invitation = client.post(f"/clubs/{club_id}/teacher-invitation", headers=scenario.auth("teacher"), json={
        "decision": "ACCEPT",
    })
    assert invitation.status_code == 404
    with SessionLocal() as db:
        assert db.get(Club, UUID(club_id)).status == ClubStatus.PENDING
        assert not db.scalars(select(ClubMembership.id).where(ClubMembership.club_id == UUID(club_id))).all()


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_rollover_yields_one_copy_and_audit(scenario, sources, monkeypatch, same_key):
    key = uuid4()
    first, second = concurrent_requests(
        monkeypatch,
        lambda client: roll(client, scenario, [sources["closed"]], key=key),
        lambda client: roll(client, scenario, [sources["closed"]], actor="admin_tab", key=key if same_key else uuid4()),
    )
    assert sorted([first.status_code, second.status_code]) == ([201, 201] if same_key else [201, 409])
    if same_key:
        assert first.json() == second.json()
    assert len(carried_clubs(scenario)) == len(closure_audits(scenario, ROLLED_OVER)) == 1


def test_closed_target_wins_race_with_rollover(client, scenario, designated, sources):
    request_id = UUID(scenario.request(client).json()["id"])
    set_request_deadline(request_id)

    def confirm(db):
        admin = db.get(Account, scenario.accounts["admin"])
        return confirm_year_closure(db, admin, scenario.years["active"], request_id)

    def carry(db):
        admin = db.get(Account, scenario.accounts["admin"])
        return rollover_clubs(db, admin, scenario.years["active"], ClubRolloverRequest(
            source_club_ids=[sources["closed"]],
        ), uuid4())

    rejected = ordered_transactions(confirm, carry, AcademicYearError)
    assert rejected.status_code == 409
    assert not carried_clubs(scenario)
    assert not closure_audits(scenario, ROLLED_OVER)


def test_rollovers_in_independent_schools_proceed_concurrently(scenario, sources):
    key = uuid4()

    def carry(db, *, actor, target, source):
        admin = db.get(Account, scenario.accounts[actor])
        return rollover_clubs(db, admin, scenario.years[target], ClubRolloverRequest(source_club_ids=[sources[source]]), key)

    receivers = [Queue(), Queue()]
    release = Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            run_transaction, lambda db: carry(db, actor="admin", target="active", source="closed"),
            receivers[0], release=release,
        )
        try:
            first_pid = receivers[0].get(timeout=WAIT)
            second = executor.submit(
                run_transaction, lambda db: carry(db, actor="other_admin", target="other", source="foreign"),
                receivers[1],
            )
            assert receivers[1].get(timeout=WAIT) != first_pid
            assert second.result(timeout=WAIT).target_year_id == scenario.years["other"]
        finally:
            release.set()
        assert first.result(timeout=WAIT).target_year_id == scenario.years["active"]
    assert len(closure_audits(scenario, ROLLED_OVER)) == 2


def test_rollover_database_failure_rolls_back_club_copies_and_audit(client, scenario, sources):
    key = uuid4()

    def abort_after_flush(db, _context):
        if any(isinstance(row, AuditLog) and row.action == ROLLED_OVER for row in db.new):
            db.execute(text("SELECT 1 / 0"))

    event.listen(Session, "after_flush", abort_after_flush)
    try:
        failed = roll(client, scenario, [sources["closed"], sources["archived"]], key=key)
    finally:
        event.remove(Session, "after_flush", abort_after_flush)
    assert failed.status_code == 503
    assert not carried_clubs(scenario)
    assert not closure_audits(scenario, ROLLED_OVER)
    assert roll(client, scenario, [sources["closed"], sources["archived"]], key=key).status_code == 201
    assert len(carried_clubs(scenario)) == 2


def test_database_rejects_duplicate_cross_school_and_rewritten_lineage(client, scenario, sources):
    result = roll(client, scenario, [sources["closed"]])
    assert result.status_code == 201
    carried_id = UUID(result.json()["items"][0]["club_id"])

    for source in (sources["closed"], sources["foreign"]):
        with pytest.raises(IntegrityError), SessionLocal.begin() as db:
            db.add(Club(
                school_id=scenario.schools["own"], academic_year_id=scenario.years["active"],
                name="Duplicate", category="STEM", rolled_over_from_club_id=source, rollover_batch_id=uuid4(),
            ))
    with pytest.raises(IntegrityError), SessionLocal.begin() as db:
        db.execute(update(Club).where(Club.id == carried_id).values(rolled_over_from_club_id=sources["archived"]))
    assert len(carried_clubs(scenario)) == 1
