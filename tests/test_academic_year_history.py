from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from attachment_samples import sample_pdf
from test_academic_year_closure_workflow import client, scenario  # noqa: F401

from app.config.database import SessionLocal
from app.main import app
from app.models.academic_year import AcademicYear
from app.models.activity import Activity
from app.models.activity_attachment import ActivityAttachment
from app.models.activity_revision import ActivityRevision
from app.models.club import Club
from app.models.enums import AcademicYearStatus, ActivityRevisionStatus, ClubStatus
from app.services.activity_attachments import attachment_object_key
from app.services.storage import get_attachment_storage
from app.services.storage.memory import MemoryAttachmentStorage


FEED = "/activities/published"
HISTORY = "/academic-years/history"


@dataclass(frozen=True)
class Publication:
    club_id: UUID
    activity_id: UUID
    revision_id: UUID

    def attachment_url(self, attachment_id: UUID) -> str:
        return f"/clubs/{self.club_id}/activities/{self.activity_id}/revisions/{self.revision_id}/attachments/{attachment_id}"


def publications_path(year_id):
    return f"/academic-years/{year_id}/published-activities"


def add_archived_year(db, school_id):
    year = AcademicYear(
        school_id=school_id, name="Archived year", start_date=date(2026, 6, 1), end_date=date(2027, 5, 31),
        status=AcademicYearStatus.ARCHIVED,
    )
    db.add(year)
    db.flush()
    return year.id


def add_publication(
    db, year_id, author_id, title, *, status=ActivityRevisionStatus.APPROVED,
    published=True, purged=False, superseded=False, club_status=ClubStatus.ACTIVE,
):
    year = db.get(AcademicYear, year_id)
    club = Club(
        school_id=year.school_id, academic_year_id=year_id, created_by=author_id,
        name="Robotics Club", category="STEM", status=club_status,
    )
    db.add(club)
    db.flush()
    activity = Activity(club_id=club.id, created_by=author_id)
    db.add(activity)
    db.flush()
    now = datetime.now(timezone.utc)
    revision = ActivityRevision(
        activity_id=activity.id,
        revision_number=1,
        status=status,
        is_published=published,
        title=None if purged else title,
        description=None if purged else "An activity description",
        category=None if purged else "STEM",
        activity_date=None if purged else date(2026, 11, 25),
        superseded_at=now if superseded else None,
        content_purged_at=now if purged else None,
    )
    db.add(revision)
    db.flush()
    return Publication(club.id, activity.id, revision.id)


def add_attachment(db, storage, publication):
    attachment_id = uuid4()
    content = sample_pdf()
    key = attachment_object_key(publication.revision_id, attachment_id)
    storage.put(key, content, "application/pdf")
    db.add(ActivityAttachment(
        id=attachment_id, revision_id=publication.revision_id, file_name="report.pdf", storage_key=key,
        file_type="application/pdf", idempotency_key=uuid4(), size_bytes=len(content),
        content_sha256=sha256(content).hexdigest(),
    ))
    return publication.attachment_url(attachment_id)


def test_feed_shows_publications_only_in_the_school_active_year(client, scenario):
    with SessionLocal.begin() as db:
        student = scenario.accounts["student"]
        visible = add_publication(db, scenario.years["active"], student, "Current activity")
        add_publication(db, scenario.years["historical"], student, "Old activity")
        add_publication(db, scenario.years["other"], scenario.accounts["other_teacher"], "Other school activity")
        add_publication(
            db, scenario.years["active"], student, "Private draft",
            status=ActivityRevisionStatus.DRAFT, published=False,
        )
        add_publication(
            db, scenario.years["active"], student, "Pending review",
            status=ActivityRevisionStatus.PENDING, published=False,
        )

    response = client.get(FEED, headers=scenario.auth("student"))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert [item["revision_id"] for item in response.json()] == [str(visible.revision_id)]


def test_closing_year_moves_its_publications_out_of_current_feed(client, scenario):
    active_year = scenario.years["active"]
    student = scenario.accounts["student"]
    with SessionLocal.begin() as db:
        old = add_publication(db, active_year, student, "Closing year activity")

    student_headers = scenario.auth("student")
    assert [item["revision_id"] for item in client.get(FEED, headers=student_headers).json()] == [str(old.revision_id)]
    with SessionLocal.begin() as db:
        db.get(AcademicYear, active_year).status = AcademicYearStatus.CLOSED

    assert client.get(FEED, headers=student_headers).json() == []
    assert [item["revision_id"] for item in client.get(
        publications_path(active_year), headers=student_headers,
    ).json()["items"]] == [str(old.revision_id)]

    new_year = client.post(
        "/academic-years",
        headers={**scenario.auth("admin"), "Idempotency-Key": str(uuid4())},
        json={"name": "New year", "start_date": "2027-06-01", "end_date": "2028-05-31"},
    )
    assert new_year.status_code == 201, new_year.text
    assert client.get(FEED, headers=student_headers).json() == []
    with SessionLocal.begin() as db:
        new = add_publication(db, UUID(new_year.json()["id"]), student, "New year activity")
    assert [item["revision_id"] for item in client.get(FEED, headers=student_headers).json()] == [str(new.revision_id)]


@pytest.mark.parametrize("actor", ["student", "teacher", "admin"])
def test_history_lists_both_historical_states_with_stable_pages(client, scenario, actor):
    with SessionLocal.begin() as db:
        archived = add_archived_year(db, scenario.schools["own"])
        add_archived_year(db, scenario.schools["other"])

    headers = scenario.auth(actor)
    first = client.get(HISTORY, params={"limit": 1}, headers=headers)
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "private, no-store"
    page = first.json()
    second = client.get(HISTORY, params={"limit": 1, "after": page["next_cursor"]}, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["next_cursor"] is None
    items = page["items"] + second.json()["items"]
    assert [item["id"] for item in items] == sorted(
        [str(archived), str(scenario.years["historical"])], reverse=True,
    )
    assert {item["status"] for item in items} == {"ARCHIVED", "CLOSED"}


@pytest.mark.parametrize("actor", ["student", "teacher"])
def test_historical_publications_keep_approved_visibility_and_retention_filters(client, scenario, actor):
    student_id = scenario.accounts["student"]
    with SessionLocal.begin() as db:
        year_id = add_archived_year(db, scenario.schools["own"])
        visible = add_publication(db, year_id, student_id, "Published")
        for status in (ActivityRevisionStatus.DRAFT, ActivityRevisionStatus.PENDING, ActivityRevisionStatus.REJECTED):
            add_publication(db, year_id, student_id, "Private history", status=status, published=False)
        add_publication(db, year_id, student_id, "Superseded", published=False, superseded=True)
        add_publication(db, year_id, student_id, "Purged", published=False, purged=True, superseded=True)
        add_publication(db, year_id, student_id, "Hidden club", club_status=ClubStatus.ARCHIVED)

    response = client.get(publications_path(year_id), headers=scenario.auth(actor))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {"items": [{
        "activity_id": str(visible.activity_id), "revision_id": str(visible.revision_id), "revision_number": 1,
        "club_id": str(visible.club_id), "club_name": "Robotics Club", "title": "Published",
        "description": "An activity description", "category": "STEM", "activity_date": "2026-11-25",
    }], "next_cursor": None}


def test_historical_publications_paginate_with_same_date_tiebreaker(client, scenario):
    year_id = scenario.years["historical"]
    with SessionLocal.begin() as db:
        revisions = [
            add_publication(db, year_id, scenario.accounts["student"], title).revision_id
            for title in ("First", "Second")
        ]

    headers = scenario.auth("teacher")
    path = publications_path(year_id)
    first = client.get(path, headers=headers, params={"limit": 1})
    assert first.status_code == 200, first.text
    page = first.json()
    second = client.get(path, headers=headers, params={"limit": 1, "after": page["next_cursor"]})
    assert second.status_code == 200, second.text
    assert second.json()["next_cursor"] is None
    items = page["items"] + second.json()["items"]
    assert [item["revision_id"] for item in items] == sorted(map(str, revisions), reverse=True)
    for endpoint in (HISTORY, path):
        assert client.get(endpoint, headers=headers, params={"limit": 51}).status_code == 422
        assert client.get(endpoint, headers=headers, params={"after": "invalid"}).status_code == 422


def test_history_enforces_role_and_school_scope(client, scenario):
    own_historical = publications_path(scenario.years["historical"])
    active = publications_path(scenario.years["active"])
    unknown = publications_path(uuid4())
    for actor in ("owner", "unassigned"):
        headers = scenario.auth(actor)
        for path in (HISTORY, own_historical, FEED):
            assert client.get(path, headers=headers).status_code == 403

    other_headers = scenario.auth("other_teacher")
    other = client.get(own_historical, headers=other_headers)
    missing = client.get(unknown, headers=other_headers)
    assert other.status_code == missing.status_code == 404
    assert other.json() == missing.json()
    assert client.get(HISTORY, headers=other_headers).json() == {"items": [], "next_cursor": None}
    assert client.get(active, headers=scenario.auth("student")).status_code == 404
    assert client.get(own_historical, headers=scenario.auth("admin")).status_code == 200


def test_archived_year_attachments_keep_existing_authorization(client, scenario, monkeypatch):
    storage = MemoryAttachmentStorage()
    monkeypatch.setitem(app.dependency_overrides, get_attachment_storage, lambda: storage)
    with SessionLocal.begin() as db:
        year_id = add_archived_year(db, scenario.schools["own"])
        student = scenario.accounts["student"]
        public = add_publication(db, year_id, student, "Published")
        draft = add_publication(db, year_id, student, "Draft", status=ActivityRevisionStatus.DRAFT, published=False)
        purged = add_publication(db, year_id, student, "Purged", published=False, purged=True, superseded=True)
        public_url = add_attachment(db, storage, public)
        draft_url = add_attachment(db, storage, draft)
        purged_url = add_attachment(db, storage, purged)

    for actor in ("student", "teacher", "admin"):
        response = client.get(public_url, headers=scenario.auth(actor))
        assert response.status_code == 200, response.text
        assert response.content == sample_pdf()
        assert client.get(purged_url, headers=scenario.auth(actor)).status_code == 404
    for actor in ("student", "teacher", "other_teacher"):
        assert client.get(draft_url, headers=scenario.auth(actor)).status_code == 404
    assert client.get(draft_url, headers=scenario.auth("admin")).status_code == 200
    assert client.get(public_url, headers=scenario.auth("other_teacher")).status_code == 404
    assert client.get(public_url, headers=scenario.auth("owner")).status_code == 403
