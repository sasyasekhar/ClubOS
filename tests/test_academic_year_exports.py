import csv
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import BytesIO, StringIO
from threading import Event
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.orm import Session

from app.config.database import SessionLocal, engine
from app.main import app
from app.models.academic_year import AcademicYear
from app.models.academic_year_export_limit import AcademicYearExportLimit
from app.models.account import Account
from app.models.activity import Activity
from app.models.activity_attachment import ActivityAttachment
from app.models.activity_review import ActivityReview
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
from app.models.auth_session import AuthSession
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.club_review import ClubReview
from app.models.enums import (
    AcademicYearStatus,
    AccountType,
    ActivityReviewDecision,
    ActivityRevisionStatus,
    ClubReviewDecision,
    ClubRole,
    ClubStatus,
    ImpersonationStatus,
    MembershipStatus,
    ReviewMode,
)
from app.models.impersonation_session import ImpersonationSession
from app.models.school import School
from app.models.school_admin import SchoolAdmin
from app.routes.auth import COOKIE_NAME
from app.services import academic_year_exports as exports
from app.services.sessions import IDLE_TIMEOUT, token_hash


COLUMNS = {
    "academic_year.csv": ["academic_year_id", "name", "start_date", "end_date", "status"],
    "clubs.csv": ["club_id", "academic_year_id", "name", "category", "status"],
    "representative_memberships.csv": [
        "membership_id", "club_id", "representative_account_id", "role", "status", "start_date", "end_date"
    ],
    "club_reviews.csv": ["review_id", "club_id", "decision", "reviewed_at"],
    "activities.csv": ["activity_id", "club_id", "created_at"],
    "activity_revisions.csv": [
        "revision_id", "activity_id", "revision_number", "title", "activity_date", "category", "status",
        "is_published", "created_at", "submitted_at", "superseded_at", "content_purged_at",
    ],
    "activity_reviews.csv": ["review_id", "revision_id", "decision", "reviewed_at", "review_mode"],
    "activity_attachments.csv": ["attachment_id", "revision_id", "file_type", "size_bytes", "uploaded_at"],
}
START = date(2025, 6, 1)
END = date(2026, 5, 31)
RECORDED = datetime(2025, 8, 1, 10, tzinfo=timezone.utc)
PASSWORD_HASH = "private-password-hash"
REVIEW_COMMENTS = "private-review-comments"
DESCRIPTION = "private-description"
STORAGE_KEY = "private/storage/object"
FILE_NAME = "../../private-attached-file.pdf"
IMPERSONATION_REASON = "private-impersonation-reason"
CONTENT_HASH = "a" * 64


@dataclass(frozen=True)
class ExportScenario:
    schools: dict[str, UUID]
    years: dict[str, UUID]
    accounts: dict[str, UUID]
    credentials: dict[str, tuple[str, str]]
    contact: str

    def auth(self, actor="admin"):
        cookie, tab_key = self.credentials[actor]
        return {"Cookie": f"{COOKIE_NAME}={cookie}", "X-ClubOS-Tab": tab_key}

    def download(self, client, year="closed", actor="admin", **kwargs):
        year_id = self.years.get(year, year)
        headers = {} if actor is None else self.auth(actor)
        return client.post(f"/academic-years/{year_id}/export", headers=headers, **kwargs)


@pytest.fixture
def scenario():
    if engine.url.get_backend_name() != "postgresql" or engine.url.database != "clubos_test":
        pytest.fail("Use the clubos_test PostgreSQL database")

    suffix = uuid4().hex[:12]
    with SessionLocal.begin() as db:
        schools = {
            name: School(name=name.title(), school_code=f"CLB-{name}-{suffix}") for name in ("own", "other")
        }
        db.add_all(schools.values())
        db.flush()

        identities = [
            ("admin", AccountType.SCHOOL_ADMIN, "own"),
            ("other_admin", AccountType.SCHOOL_ADMIN, "other"),
            ("unassigned", AccountType.SCHOOL_ADMIN, "own"),
            ("student", AccountType.STUDENT, "own"),
            ("teacher", AccountType.TEACHER, "own"),
            ("former_teacher", AccountType.TEACHER, "own"),
            ("other_student", AccountType.STUDENT, "other"),
            ("other_teacher", AccountType.TEACHER, "other"),
            ("owner", AccountType.PLATFORM_OWNER, None),
        ]
        accounts = {
            name: Account(
                school_id=schools[school].id if school else None,
                account_type=role,
                full_name=f"Private {name} name",
                username=f"{name}_{suffix}",
                email=f"{name}_{suffix}@example.com",
                password_hash=PASSWORD_HASH,
                is_active=name != "former_teacher",
            )
            for name, role, school in identities
        }
        years = {
            name: AcademicYear(
                school_id=schools[school].id,
                name=f"{name.title()} 2025–2026",
                start_date=START,
                end_date=END,
                status=year_status,
            )
            for name, school, year_status in (
                ("closed", "own", AcademicYearStatus.CLOSED),
                ("active", "own", AcademicYearStatus.ACTIVE),
                ("archived", "own", AcademicYearStatus.ARCHIVED),
                ("other", "other", AcademicYearStatus.ACTIVE),
            )
        }
        db.add_all([*accounts.values(), *years.values()])
        db.flush()
        db.add_all([
            SchoolAdmin(school_id=schools["own"].id, account_id=accounts["admin"].id),
            SchoolAdmin(school_id=schools["other"].id, account_id=accounts["other_admin"].id),
        ])
        session_accounts = {**accounts, "second_tab": accounts["admin"]}
        credentials = {name: (uuid4().hex, uuid4().hex) for name in session_accounts}
        db.add_all(
            AuthSession(
                account_id=account.id,
                token_hash=token_hash(*credentials[name]),
                expires_at=datetime.now(timezone.utc) + IDLE_TIMEOUT,
            )
            for name, account in session_accounts.items()
        )
        result = ExportScenario(
            {name: school.id for name, school in schools.items()},
            {name: year.id for name, year in years.items()},
            {name: account.id for name, account in accounts.items()},
            credentials,
            accounts["student"].email,
        )

    try:
        yield result
    finally:
        school_ids = tuple(result.schools.values())
        account_ids = tuple(result.accounts.values())
        clubs = select(Club.id).where(Club.school_id.in_(school_ids))
        activities = select(Activity.id).where(Activity.club_id.in_(clubs))
        revisions = select(ActivityRevision.id).where(ActivityRevision.activity_id.in_(activities))
        with SessionLocal.begin() as db:
            db.execute(delete(AuditLog).where(AuditLog.actor_account_id.in_(account_ids)))
            db.execute(delete(ActivityReview).where(ActivityReview.revision_id.in_(revisions)))
            db.execute(delete(ImpersonationSession).where(ImpersonationSession.admin_account_id.in_(account_ids)))
            db.execute(delete(ActivityAttachment).where(ActivityAttachment.revision_id.in_(revisions)))
            db.execute(delete(ActivityRevision).where(ActivityRevision.activity_id.in_(activities)))
            db.execute(delete(Activity).where(Activity.club_id.in_(clubs)))
            db.execute(delete(ClubReview).where(ClubReview.club_id.in_(clubs)))
            db.execute(delete(ClubMembership).where(ClubMembership.club_id.in_(clubs)))
            db.execute(delete(Club).where(Club.school_id.in_(school_ids)))
            db.execute(delete(AcademicYear).where(AcademicYear.school_id.in_(school_ids)))
            db.execute(delete(SchoolAdmin).where(SchoolAdmin.school_id.in_(school_ids)))
            db.execute(delete(Account).where(Account.id.in_(account_ids)))
            db.execute(delete(School).where(School.id.in_(school_ids)))


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def add_club(db, scenario, year="closed", name=None):
    school = "other" if year == "other" else "own"
    student = "other_student" if school == "other" else "student"
    club = Club(
        school_id=scenario.schools[school],
        academic_year_id=scenario.years[year],
        created_by=scenario.accounts[student],
        name=name or f"{year.title()} Robotics Club",
        description=DESCRIPTION,
        category="STEM",
        logo_key=STORAGE_KEY,
        status=ClubStatus.ACTIVE,
    )
    db.add(club)
    db.flush()
    return club


def add_activity(db, club, author_id):
    activity = Activity(club_id=club.id, created_by=author_id, created_at=RECORDED)
    db.add(activity)
    db.flush()
    return activity


def add_revision(db, activity, number, **overrides):
    revision = ActivityRevision(**{
        "activity_id": activity.id,
        "revision_number": number,
        "title": "Robotics, demonstration",
        "activity_date": RECORDED.date(),
        "description": DESCRIPTION,
        "category": "STEM",
        "status": ActivityRevisionStatus.APPROVED,
        "created_at": RECORDED,
        "submitted_at": RECORDED,
        **overrides,
    })
    db.add(revision)
    db.flush()
    return revision


def populate_year(db, scenario, year="closed", *, impersonated=False):
    club = add_club(db, scenario, year)
    prefix = "other_" if year == "other" else ""
    student_id = scenario.accounts[f"{prefix}student"]
    teacher_id = scenario.accounts[f"{prefix}teacher"]
    admin_id = scenario.accounts[f"{prefix}admin"]
    former_id = teacher_id if prefix else scenario.accounts["former_teacher"]

    memberships = [
        ClubMembership(
            club_id=club.id, account_id=former_id, role=ClubRole.TEACHER_REP,
            status=MembershipStatus.ENDED, start_date=START, end_date=RECORDED.date(),
        ),
        ClubMembership(
            club_id=club.id, account_id=teacher_id, role=ClubRole.TEACHER_REP,
            status=MembershipStatus.PENDING, start_date=RECORDED.date(),
        ),
        ClubMembership(
            club_id=club.id, account_id=student_id, role=ClubRole.STUDENT_REP,
            status=MembershipStatus.ACTIVE, start_date=START,
        ),
    ]
    club_review = ClubReview(
        club_id=club.id, admin_account_id=admin_id, decision=ClubReviewDecision.APPROVED,
        comments=REVIEW_COMMENTS, reviewed_at=RECORDED,
    )
    db.add_all([*memberships, club_review])
    activity = add_activity(db, club, student_id)
    purged = add_revision(
        db, activity, 1, title=None, activity_date=None, description=None, category=None,
        superseded_at=RECORDED, content_purged_at=RECORDED + timedelta(days=30),
    )
    changes_requested = add_revision(db, activity, 2, status=ActivityRevisionStatus.CHANGES_REQUESTED)
    published = add_revision(db, activity, 3, is_published=True)
    draft = add_revision(db, activity, 4, status=ActivityRevisionStatus.DRAFT, submitted_at=None)
    activity_reviews = [
        ActivityReview(
            revision_id=revision.id, actor_account_id=teacher_id, review_mode=ReviewMode.DIRECT,
            decision=decision, comments=REVIEW_COMMENTS, reviewed_at=RECORDED,
        )
        for revision, decision in (
            (purged, ActivityReviewDecision.APPROVED),
            (changes_requested, ActivityReviewDecision.CHANGES_REQUESTED),
        )
    ]
    impersonation = None
    if impersonated:
        impersonation = ImpersonationSession(
            admin_account_id=scenario.accounts["owner"], target_account_id=teacher_id,
            reason=IMPERSONATION_REASON, status=ImpersonationStatus.ENDED,
        )
        db.add(impersonation)
        db.flush()
        activity_reviews.append(ActivityReview(
            revision_id=published.id, actor_account_id=impersonation.admin_account_id,
            represented_account_id=teacher_id, impersonation_session_id=impersonation.id,
            review_mode=ReviewMode.IMPERSONATION, decision=ActivityReviewDecision.APPROVED,
            reviewed_at=RECORDED, comments=REVIEW_COMMENTS,
        ))
    attachments = [
        ActivityAttachment(
            revision_id=published.id, file_name=FILE_NAME, storage_key=STORAGE_KEY,
            file_type="application/pdf", size_bytes=123, content_sha256=CONTENT_HASH,
            idempotency_key=uuid4(), uploaded_at=RECORDED,
        ),
        ActivityAttachment(
            revision_id=changes_requested.id, file_name=FILE_NAME, storage_key=STORAGE_KEY,
            file_type="image/png", uploaded_at=RECORDED,
        ),
    ]
    db.add_all([*activity_reviews, *attachments])
    db.flush()
    return {
        "clubs.csv": [club],
        "representative_memberships.csv": memberships,
        "club_reviews.csv": [club_review],
        "activities.csv": [activity],
        "activity_revisions.csv": [purged, changes_requested, published, draft],
        "activity_reviews.csv": activity_reviews,
        "activity_attachments.csv": attachments,
        "impersonation": impersonation,
    }


def read_export(response):
    assert response.status_code == 200, response.text
    with ZipFile(BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        assert sorted(archive.namelist()) == sorted([*COLUMNS, "manifest.json"])
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema_version"] == 1
        assert datetime.fromisoformat(manifest["generated_at"]).utcoffset() == timedelta(0)
        files = {entry["name"]: entry for entry in manifest["files"]}
        assert files.keys() == COLUMNS.keys()
        rows = {}
        for filename, columns in COLUMNS.items():
            reader = csv.DictReader(StringIO(archive.read(filename).decode("utf-8"), newline=""))
            assert reader.fieldnames == files[filename]["columns"] == columns
            rows[filename] = list(reader)
            assert len(rows[filename]) == files[filename]["rows"]
            assert files[filename]["description"]
    return rows, manifest


def export_audits(scenario):
    with SessionLocal() as db:
        return list(db.scalars(select(AuditLog).where(
            AuditLog.actor_account_id.in_(scenario.accounts.values()),
            AuditLog.action == exports.EXPORT_ACTION,
        )))


def expire_export_limit(scenario):
    with SessionLocal.begin() as db:
        db.execute(update(AcademicYearExportLimit).where(
            AcademicYearExportLimit.school_id == scenario.schools["own"],
        ).values(last_started_at=RECORDED))


def test_successful_download_and_audit(client, scenario, monkeypatch):
    with SessionLocal.begin() as db:
        data = populate_year(db, scenario)
        club_id = data["clubs.csv"][0].id
    opened = []
    temporary_file = exports.TemporaryFile

    def track_file(**options):
        handle = temporary_file(**options)
        opened.append(handle)
        return handle

    monkeypatch.setattr(exports, "TemporaryFile", track_file)
    response = scenario.download(client)
    rows, manifest = read_export(response)
    assert len(opened) == 1 and opened[0].closed
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == f'attachment; filename="academic-year-{scenario.years["closed"]}.zip"'
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert int(response.headers["content-length"]) == len(response.content)
    assert manifest["scope"] == {
        "school_id": str(scenario.schools["own"]), "academic_year_id": str(scenario.years["closed"])
    }
    assert rows["academic_year.csv"] == [{
        "academic_year_id": str(scenario.years["closed"]), "name": "Closed 2025–2026",
        "start_date": START.isoformat(), "end_date": END.isoformat(), "status": "CLOSED",
    }]
    assert rows["clubs.csv"] == [{
        "club_id": str(club_id), "academic_year_id": str(scenario.years["closed"]),
        "name": "Closed Robotics Club", "category": "STEM", "status": "ACTIVE",
    }]
    assert rows["club_reviews.csv"][0]["decision"] == "APPROVED"
    assert rows["activities.csv"][0]["club_id"] == str(club_id)
    assert rows["activities.csv"][0]["created_at"] == RECORDED.isoformat()

    audit, = export_audits(scenario)
    assert audit.actor_account_id == scenario.accounts["admin"]
    assert audit.entity_type == "academic_year"
    assert audit.entity_id == str(scenario.years["closed"])
    assert audit.details == {"school_id": str(scenario.schools["own"]), "row_count": 15}
    assert audit.occurred_at == datetime.fromisoformat(manifest["generated_at"])
    assert audit.impersonation_session_id is None
    with SessionLocal() as db:
        assert db.get(AcademicYear, scenario.years["closed"]).status is AcademicYearStatus.CLOSED
        assert db.get(AcademicYear, scenario.years["active"]).status is AcademicYearStatus.ACTIVE
        assert db.get(Club, club_id).status is ClubStatus.ACTIVE


def test_export_follows_the_selected_school_and_year_relationships(client, scenario):
    with SessionLocal.begin() as db:
        own = populate_year(db, scenario, impersonated=True)
        populate_year(db, scenario, "active")
        populate_year(db, scenario, "other")
        club = own["clubs.csv"][0]
        foreign_activity = add_activity(db, club, scenario.accounts["other_student"])
        add_revision(db, foreign_activity, 1)
        db.add_all([
            ClubMembership(
                club_id=club.id, account_id=scenario.accounts["other_teacher"], role=ClubRole.TEACHER_REP,
                status=MembershipStatus.ENDED, start_date=START, end_date=END,
            ),
            ClubReview(
                club_id=club.id, admin_account_id=scenario.accounts["other_admin"],
                decision=ClubReviewDecision.REJECTED,
            ),
            ActivityReview(
                revision_id=own["activity_revisions.csv"][0].id,
                actor_account_id=scenario.accounts["other_teacher"], review_mode=ReviewMode.DIRECT,
                decision=ActivityReviewDecision.REJECTED,
            ),
        ])
        expected = {
            name: {str(row.id) for row in own[name]} for name in COLUMNS if name != "academic_year.csv"
        }

    forged_scope = {"school_id": str(scenario.schools["other"])}
    rows, _ = read_export(scenario.download(client, params=forged_scope, json=forged_scope))
    for name, ids in expected.items():
        assert {row[COLUMNS[name][0]] for row in rows[name]} == ids
    assert all(str(scenario.accounts["other_teacher"]) not in str(row) for row in rows.values())


def test_cross_school_year_is_indistinguishable_from_unknown_year(client, scenario):
    forged_scope = {"school_id": str(scenario.schools["other"])}
    denied = scenario.download(client, "other", params=forged_scope, json=forged_scope)
    unknown = scenario.download(client, uuid4())
    assert denied.status_code == unknown.status_code == 404
    assert denied.json() == unknown.json() == {"detail": "Academic year not found."}
    assert export_audits(scenario) == []


@pytest.mark.parametrize("actor", ["student", "teacher", "owner", "unassigned", "former_teacher"])
def test_role_and_assignment_required(client, scenario, monkeypatch, actor):
    def unexpected_archive(**options):
        pytest.fail("A restricted request reached temporary archive creation")

    monkeypatch.setattr(exports, "TemporaryFile", unexpected_archive)
    response = scenario.download(client, actor=actor)
    assert response.status_code == (401 if actor == "former_teacher" else 403)
    assert export_audits(scenario) == []


def test_export_requires_the_cookie_and_tab_key(client, scenario):
    path = f"/academic-years/{scenario.years['closed']}/export"
    assert client.post(path).status_code == 401
    for header in scenario.auth().items():
        assert client.post(path, headers=dict([header])).status_code == 401
    assert export_audits(scenario) == []


@pytest.mark.parametrize("year", ["active", "closed", "archived"])
def test_empty_years_include_metadata_and_headers(client, scenario, year):
    rows, _ = read_export(scenario.download(client, year))
    assert rows.pop("academic_year.csv")[0]["status"] == year.upper()
    assert rows == dict.fromkeys(rows, [])


def test_exports_history_including_inactive_members_and_purged_revisions(client, scenario):
    with SessionLocal.begin() as db:
        populate_year(db, scenario, impersonated=True)

    rows, _ = read_export(scenario.download(client))
    memberships = {row["status"]: row for row in rows["representative_memberships.csv"]}
    assert memberships.keys() == {"ACTIVE", "PENDING", "ENDED"}
    assert memberships["ENDED"]["representative_account_id"] == str(scenario.accounts["former_teacher"])
    assert memberships["ENDED"]["start_date"] == START.isoformat()
    assert memberships["ENDED"]["end_date"] == RECORDED.date().isoformat()
    assert memberships["PENDING"]["end_date"] == ""

    revisions = rows["activity_revisions.csv"]
    assert [row["status"] for row in revisions] == ["APPROVED", "CHANGES_REQUESTED", "APPROVED", "DRAFT"]
    assert [row["revision_number"] for row in revisions] == ["1", "2", "3", "4"]
    assert all(revisions[0][field] == "" for field in ("title", "activity_date", "category"))
    assert revisions[0]["content_purged_at"] == (RECORDED + timedelta(days=30)).isoformat()
    assert revisions[2]["title"] == "Robotics, demonstration"
    assert revisions[2]["is_published"] == "true"
    assert revisions[3]["submitted_at"] == ""
    reviews = {row["revision_id"]: row["decision"] for row in rows["activity_reviews.csv"]}
    assert reviews == {row["revision_id"]: row["status"] for row in revisions[:3]}
    provenance = {row["revision_id"]: row["review_mode"] for row in rows["activity_reviews.csv"]}
    assert [provenance[row["revision_id"]] for row in revisions[:3]] == ["DIRECT", "DIRECT", "IMPERSONATION"]
    attachments = {row["revision_id"]: row for row in rows["activity_attachments.csv"]}
    assert attachments[revisions[1]["revision_id"]]["size_bytes"] == ""
    assert attachments[revisions[2]["revision_id"]]["size_bytes"] == "123"
    assert attachments[revisions[2]["revision_id"]]["file_type"] == "application/pdf"


def test_export_omits_private_fields_and_values(client, scenario):
    with SessionLocal.begin() as db:
        data = populate_year(db, scenario, impersonated=True)
        impersonation_id = str(data["impersonation"].id)
        attachment_key = str(data["activity_attachments.csv"][0].idempotency_key)

    response = scenario.download(client)
    read_export(response)
    with ZipFile(BytesIO(response.content)) as archive:
        exported = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())
    cookie, tab_key = scenario.credentials["admin"]
    for private in (
        cookie, tab_key, token_hash(cookie, tab_key), PASSWORD_HASH, scenario.contact, "Private student name",
        REVIEW_COMMENTS, DESCRIPTION, STORAGE_KEY, FILE_NAME, CONTENT_HASH, attachment_key,
        str(scenario.accounts["owner"]), impersonation_id, IMPERSONATION_REASON,
    ):
        assert private not in exported
    for field in ("password", "token_hash", "storage_key", "content_sha256", "impersonation_session_id", "file_name"):
        assert field not in exported


def test_csv_formula_protection_and_escaping(client, scenario):
    names = [
        "=1+1", "+1+1", "-1+1", "@SUM(1,2)", "\tdata", "\rdata", "\ndata",
        "  \u200b＝1+1", "\u00a0＠SUM(1,2)", "\x1f=1+1", 'Robotics, "demo"\r\n続き',
    ]
    with SessionLocal.begin() as db:
        for name in names:
            add_club(db, scenario, name=name)
        db.get(AcademicYear, scenario.years["closed"]).name = " +1+1"

    rows, _ = read_export(scenario.download(client))
    assert {row["name"] for row in rows["clubs.csv"]} == {
        *("'" + name.replace("\x1f", "") for name in names[:-1]), names[-1],
    }
    assert rows["academic_year.csv"][0]["name"] == "' +1+1"


def test_repeated_exports_observe_school_cooldown(client, scenario):
    assert scenario.download(client).status_code == 200
    for year, actor in (("closed", "admin"), ("active", "second_tab")):
        response = scenario.download(client, year, actor)
        assert response.status_code == 429
        assert response.headers["retry-after"] == str(exports.EXPORT_COOLDOWN_SECONDS)
    assert len(export_audits(scenario)) == 1
    assert scenario.download(client, "other", "other_admin").status_code == 200

    expire_export_limit(scenario)
    assert scenario.download(client, "active", "second_tab").status_code == 200
    assert len(export_audits(scenario)) == 3


def test_concurrent_exports_use_one_school_lock_and_a_consistent_snapshot(scenario, monkeypatch):
    ready = Event()
    resume = Event()
    write_archive = exports._write_archive

    def slow_archive(db, output, school_id, year_id):
        ready.set()
        assert resume.wait(timeout=10)
        return write_archive(db, output, school_id, year_id)

    def download():
        with TestClient(app) as client:
            return scenario.download(client)

    monkeypatch.setattr(exports, "_write_archive", slow_archive)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(download)
        try:
            assert ready.wait(timeout=10)
            expire_export_limit(scenario)
            with TestClient(app) as client:
                # Use the same tab to exercise release of the authentication lock.
                response = scenario.download(client, "active")
                assert response.status_code == 429
                assert "already running" in response.json()["detail"]
            with SessionLocal.begin() as db:
                add_club(db, scenario)
        finally:
            resume.set()
        rows, _ = read_export(first.result(timeout=15))
    assert rows["clubs.csv"] == []
    assert len(export_audits(scenario)) == 1


def test_large_export_spans_multiple_database_batches(client, scenario):
    count = exports.FETCH_ROWS * 3 + 1
    with SessionLocal.begin() as db:
        club = add_club(db, scenario)
        db.execute(insert(ClubMembership), [
            {
                "club_id": club.id, "account_id": scenario.accounts["former_teacher"],
                "role": ClubRole.TEACHER_REP, "status": MembershipStatus.ENDED, "start_date": START,
                "end_date": END,
            }
            for _ in range(count)
        ])

    rows, _ = read_export(scenario.download(client))
    assert len({row["membership_id"] for row in rows["representative_memberships.csv"]}) == count
    assert export_audits(scenario)[0].details["row_count"] == count + 2


@pytest.mark.parametrize(
    "setting,limit,status_code",
    [("MAX_EXPORT_ROWS", 2, 413), ("MAX_EXPORT_BYTES", 2048, 413), ("MAX_EXPORT_SECONDS", 0, 503)],
)
def test_export_limits_return_an_error_and_release_the_lock(client, scenario, monkeypatch, setting, limit, status_code):
    with SessionLocal.begin() as db:
        populate_year(db, scenario)
    with monkeypatch.context() as patch:
        patch.setattr(exports, setting, limit)
        response = scenario.download(client)
    assert response.status_code == status_code
    assert response.headers["content-type"] == "application/json"
    assert "content-disposition" not in response.headers
    assert export_audits(scenario) == []
    assert scenario.download(client).status_code == 429
    expire_export_limit(scenario)
    read_export(scenario.download(client))


@pytest.mark.parametrize("failure", ["disk", "audit"])
def test_failure_closes_the_archive_and_never_returns_a_download(client, scenario, monkeypatch, caplog, failure):
    opened = []
    temporary_file = exports.TemporaryFile
    write_csv = exports._write_csv

    def track_file(**options):
        handle = temporary_file(**options)
        opened.append(handle)
        return handle

    def fail_write(db, archive, filename, query, **limits):
        if filename == "clubs.csv":
            raise OSError("private/failing/path")
        return write_csv(db, archive, filename, query, **limits)

    def fail_audit(db, *_):
        if any(isinstance(row, AuditLog) and row.action == exports.EXPORT_ACTION for row in db.new):
            db.execute(text("SELECT 1 / 0"))

    with monkeypatch.context() as patch:
        patch.setattr(exports, "TemporaryFile", track_file)
        if failure == "disk":
            patch.setattr(exports, "_write_csv", fail_write)
            response = scenario.download(client)
        else:
            event.listen(Session, "before_flush", fail_audit)
            try:
                response = scenario.download(client)
            finally:
                event.remove(Session, "before_flush", fail_audit)

    assert response.status_code == 503
    assert response.json() == {"detail": "Academic-year export is temporarily unavailable."}
    assert "content-disposition" not in response.headers
    assert "private/failing/path" not in caplog.text
    assert len(opened) == 1 and opened[0].closed
    assert export_audits(scenario) == []
    assert scenario.download(client).status_code == 429
    expire_export_limit(scenario)
    read_export(scenario.download(client))
