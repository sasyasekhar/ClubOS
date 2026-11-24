import csv
from io import BytesIO, StringIO
from uuid import UUID, uuid4
from zipfile import ZipFile

from sqlalchemy import select

from app.config.database import SessionLocal
from app.models.activity import Activity
from app.models.activity_revision import ActivityRevision
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.enums import ActivityRevisionStatus, ClubStatus
from test_academic_year_closure_workflow import START, client, designated, scenario, set_request_deadline


def test_rollover_exports_lineage_and_keeps_old_activities_in_history(client, scenario):
    source_year = scenario.years["historical"]
    target_year = scenario.years["active"]
    admin = scenario.auth("admin")
    with SessionLocal.begin() as db:
        source = Club(
            school_id=scenario.schools["own"], academic_year_id=source_year,
            created_by=scenario.accounts["student"], name="Historical workshop", category="STEM",
            description="Club description", status=ClubStatus.ACTIVE, logo_key="private/historical-logo",
        )
        db.add(source)
        db.flush()
        activity = Activity(club_id=source.id, created_by=scenario.accounts["student"])
        db.add(activity)
        db.flush()
        revision = ActivityRevision(
            activity_id=activity.id, revision_number=1, status=ActivityRevisionStatus.APPROVED,
            is_published=True, title="An earlier workshop", description="Historical report",
            category="STEM", activity_date=START,
        )
        db.add(revision)
        db.flush()
        source_id, published_id = source.id, revision.id

    rollover = client.post(
        f"/academic-years/{target_year}/rollover-clubs", headers={**admin, "Idempotency-Key": str(uuid4())},
        json={"source_club_ids": [str(source_id)]},
    )
    assert rollover.status_code == 201, rollover.text
    carried_id = rollover.json()["items"][0]["club_id"]
    approval = client.post(
        f"/clubs/{carried_id}/reviews", headers={**admin, "Idempotency-Key": str(uuid4())},
        json={"decision": "APPROVED"},
    )
    assert approval.status_code == 409
    assert "fresh representative assignments" in approval.json()["detail"]

    student = scenario.auth("student")
    create_activity = client.post(
        f"/clubs/{carried_id}/activities", headers={**student, "Idempotency-Key": str(uuid4())},
        json={"title": "New report", "description": "New year", "category": "STEM", "activity_date": START.isoformat()},
    )
    assert create_activity.status_code == 409
    assert client.get("/activities/published", headers=student).json() == []
    history = client.get(f"/academic-years/{source_year}/published-activities", headers=student)
    assert history.status_code == 200, history.text
    assert [row["revision_id"] for row in history.json()["items"]] == [str(published_id)]

    export = client.post(f"/academic-years/{target_year}/export", headers=admin)
    assert export.status_code == 200, export.text
    with ZipFile(BytesIO(export.content)) as archive:
        lineage = archive.read("club_rollover_lineage.csv").decode("utf-8")
        assert list(csv.DictReader(StringIO(lineage))) == [{
            "club_id": carried_id, "source_club_id": str(source_id), "source_academic_year_id": str(source_year),
        }]
        assert b"private/historical-logo" not in lineage.encode()

    with SessionLocal() as db:
        carried = db.get(Club, UUID(carried_id))
        assert carried.status == ClubStatus.PENDING
        assert carried.logo_key is None
        assert db.scalar(select(ClubMembership.id).where(ClubMembership.club_id == carried.id)) is None
        assert db.scalar(select(Activity.id).where(Activity.club_id == carried.id)) is None


def test_closure_confirmation_replays_after_archive_and_designation_persists(client, scenario, designated):
    requested = scenario.request(client)
    assert requested.status_code == 201, requested.text
    request_id = requested.json()["id"]
    set_request_deadline(request_id)
    confirmed = scenario.confirm(client, request_id)
    assert confirmed.status_code == 200, confirmed.text

    year_path = f"/academic-years/{scenario.years['active']}"
    admin = scenario.auth("admin")
    assert client.post(f"{year_path}/export", headers=admin).status_code == 200
    archived = client.post(
        f"{year_path}/archive", headers={**admin, "Idempotency-Key": str(uuid4())}, json={"export_saved": True},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "ARCHIVED"
    assert scenario.confirm(client, request_id).json() == confirmed.json()
    original_years = client.get("/academic-years", headers=admin).json()
    assert {row["status"] for row in original_years} == {"CLOSED", "ARCHIVED"}

    next_year = client.post(
        "/academic-years", headers={**admin, "Idempotency-Key": str(uuid4())},
        json={"name": "Next year", "start_date": "2027-06-01", "end_date": "2028-05-31"},
    )
    assert next_year.status_code == 201, next_year.text
    assert client.get("/academic-years/closure-teacher", headers=admin).json() == {
        "teacher_account_id": str(scenario.accounts["teacher"]),
    }
    activated = client.post(
        f"/academic-years/{next_year.json()['id']}/closure-request",
        headers={**scenario.auth("teacher"), "Idempotency-Key": str(uuid4())},
    )
    assert activated.status_code == 201, activated.text
