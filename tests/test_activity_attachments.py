from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
from queue import Queue
from threading import Barrier, Event
from time import monotonic, sleep
from unittest.mock import Mock
from urllib.parse import quote
from uuid import UUID, uuid4

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config.database import SessionLocal, engine, get_db
from app.main import app
from app.models.account import Account
from app.models.academic_year import AcademicYear
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
    ActivityRevisionStatus,
    ClubRole,
    ClubStatus,
    MembershipStatus,
)
from app.models.school import School
from app.models.school_admin import SchoolAdmin
from app.routes.auth import COOKIE_NAME, require_account
from app.services import activity_attachments
from app.services.activity_attachments import reconcile_orphaned_upload
from app.services.attachment_policy import MAX_FILE_SIZE
from app.services.revision_retention import purge_expired_superseded_content
from app.services.sessions import IDLE_TIMEOUT, token_hash
from app.services.storage import ObjectMissing, StorageError, get_attachment_storage
from app.services.storage.memory import MemoryAttachmentStorage
from attachment_samples import sample_pdf


@dataclass(frozen=True)
class AttachmentScenario:
    club_id: UUID
    year_id: UUID
    activity_id: UUID
    revision_id: UUID
    accounts: dict[str, UUID]
    credentials: dict[str, tuple[str, str]]

    def revision_url(self, revision_id: UUID | None = None) -> str:
        return (
            f"/clubs/{self.club_id}/activities/{self.activity_id}"
            f"/revisions/{revision_id or self.revision_id}"
        )

    def attachment_url(self, attachment_id: UUID | str, revision_id: UUID | None = None) -> str:
        return f"{self.revision_url(revision_id)}/attachments/{attachment_id}"

    def auth(self, actor: str = "student") -> dict[str, str]:
        cookie, tab = self.credentials[actor]
        return {"Cookie": f"{COOKIE_NAME}={cookie}", "X-ClubOS-Tab": tab}


@pytest.fixture
def scenario():
    if engine.url.get_backend_name() != "postgresql" or engine.url.database != "clubos_test":
        pytest.fail("Use the clubos_test PostgreSQL database")

    suffix = uuid4().hex[:12]
    today = date.today()
    with SessionLocal.begin() as db:
        school = School(name="Attachment School", school_code=f"CLB-{suffix}")
        other_school = School(name="Other Attachment School", school_code=f"CLB-{suffix}X")
        db.add_all([school, other_school])
        db.flush()
        school_ids = [school.id, other_school.id]

        identities = (
            ("student", AccountType.STUDENT, school.id),
            ("other", AccountType.STUDENT, school.id),
            ("inactive", AccountType.STUDENT, school.id),
            ("teacher", AccountType.TEACHER, school.id),
            ("other_teacher", AccountType.TEACHER, school.id),
            ("admin", AccountType.SCHOOL_ADMIN, school.id),
            ("outsider", AccountType.STUDENT, other_school.id),
            ("owner", AccountType.PLATFORM_OWNER, None),
        )
        accounts = {
            name: Account(
                school_id=school_id,
                account_type=role,
                full_name=name,
                username=f"{name}_{suffix}",
                email=f"{name}_{suffix}@example.com",
                password_hash="test-only",
                is_active=name != "inactive",
            )
            for name, role, school_id in identities
        }
        year = AcademicYear(
            school_id=school.id,
            name="Attachment Year",
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=30),
            status=AcademicYearStatus.ACTIVE,
        )
        db.add_all([year, *accounts.values()])
        db.flush()

        credentials = {name: (uuid4().hex, uuid4().hex) for name in accounts}
        expires_at = datetime.now(timezone.utc) + IDLE_TIMEOUT
        db.add_all(
            AuthSession(
                account_id=account.id,
                token_hash=token_hash(*credentials[name]),
                expires_at=expires_at,
            )
            for name, account in accounts.items()
        )
        db.add(SchoolAdmin(school_id=school.id, account_id=accounts["admin"].id))

        club = Club(
            school_id=school.id,
            academic_year_id=year.id,
            created_by=accounts["student"].id,
            name="Robotics",
            category="STEM",
            status=ClubStatus.ACTIVE,
        )
        db.add(club)
        db.flush()

        db.add_all(
            ClubMembership(
                club_id=club.id,
                account_id=accounts[name].id,
                role=role,
                status=MembershipStatus.ACTIVE,
                start_date=today,
            )
            for name, role in (("student", ClubRole.STUDENT_REP), ("teacher", ClubRole.TEACHER_REP))
        )
        activity = Activity(club_id=club.id, created_by=accounts["student"].id)
        db.add(activity)
        db.flush()
        revision = ActivityRevision(
            activity_id=activity.id,
            revision_number=1,
            title="Robotics Workshop",
            activity_date=today,
            description="Test activity",
            category="STEM",
            status=ActivityRevisionStatus.DRAFT,
            is_published=False,
        )
        db.add(revision)
        db.flush()
        result = AttachmentScenario(
            club_id=club.id,
            year_id=year.id,
            activity_id=activity.id,
            revision_id=revision.id,
            accounts={name: account.id for name, account in accounts.items()},
            credentials=credentials,
        )

    try:
        yield result
    finally:
        with SessionLocal.begin() as db:
            revisions = select(ActivityRevision.id).where(
                ActivityRevision.activity_id == result.activity_id
            )
            for model in (ActivityAttachment, ActivityReview):
                db.execute(delete(model).where(model.revision_id.in_(revisions)))
            account_ids = list(result.accounts.values())
            db.execute(delete(AuditLog).where(AuditLog.actor_account_id.in_(account_ids)))
            db.execute(delete(ActivityRevision).where(
                ActivityRevision.activity_id == result.activity_id
            ))
            db.execute(delete(Activity).where(Activity.id == result.activity_id))
            db.execute(delete(ClubMembership).where(ClubMembership.club_id == result.club_id))
            db.execute(delete(Club).where(Club.id == result.club_id))
            db.execute(delete(SchoolAdmin).where(SchoolAdmin.school_id.in_(school_ids)))
            db.execute(delete(AcademicYear).where(AcademicYear.id == result.year_id))
            db.execute(delete(Account).where(Account.id.in_(account_ids)))
            db.execute(delete(School).where(School.id.in_(school_ids)))


@pytest.fixture
def memory_storage():
    return MemoryAttachmentStorage()


@pytest.fixture
def storage(monkeypatch, memory_storage):
    backend = Mock(spec=MemoryAttachmentStorage, wraps=memory_storage)
    monkeypatch.setitem(app.dependency_overrides, get_attachment_storage, lambda: backend)
    return backend


@pytest.fixture
def client(storage):
    with TestClient(app) as http_client:
        yield http_client


def upload(
    client: TestClient,
    scenario: AttachmentScenario,
    *,
    key: UUID | None = None,
    actor: str = "student",
    revision_id: UUID | None = None,
    name: str = "report.pdf",
    content: bytes | None = None,
) -> Response:
    return client.post(
        f"{scenario.revision_url(revision_id)}/attachments",
        headers={**scenario.auth(actor), "Idempotency-Key": str(key or uuid4())},
        files={"file": (name, sample_pdf() if content is None else content, "application/pdf")},
    )


def attachments(
    scenario: AttachmentScenario, revision_id: UUID | None = None
) -> list[ActivityAttachment]:
    with SessionLocal() as db:
        return db.scalars(select(ActivityAttachment).where(
            ActivityAttachment.revision_id == (revision_id or scenario.revision_id)
        )).all()


def publish(
    client: TestClient, scenario: AttachmentScenario, revision_id: UUID | None = None
) -> UUID:
    path = scenario.revision_url(revision_id)
    assert client.post(f"{path}/submit", headers=scenario.auth()).status_code == 200
    review = client.post(
        f"{path}/review",
        json={"decision": "APPROVED"},
        headers={**scenario.auth("teacher"), "Idempotency-Key": str(uuid4())},
    )
    assert review.status_code == 201, review.text
    return UUID(review.json()["review_id"])


def test_upload_saves_private_metadata_and_requires_session(client, scenario, storage):
    upload_path = f"{scenario.revision_url()}/attachments"
    files = {"file": ("report.pdf", sample_pdf(), "application/pdf")}
    headers = {**scenario.auth(), "Idempotency-Key": str(uuid4())}
    without_tab = {key: value for key, value in headers.items() if key != "X-ClubOS-Tab"}
    assert client.post(upload_path, headers=without_tab, files=files).status_code == 401
    storage.put.assert_not_called()

    response = client.post(upload_path, headers=headers, files=files)
    assert response.status_code == 201, response.text
    attachment, = attachments(scenario)
    assert response.json() == {
        "id": str(attachment.id),
        "revision_id": str(scenario.revision_id),
        "file_name": "report.pdf",
        "file_type": "application/pdf",
    }
    assert attachment.revision_id == scenario.revision_id
    assert attachment.idempotency_key == UUID(headers["Idempotency-Key"])
    assert attachment.size_bytes == len(sample_pdf())
    assert attachment.content_sha256 == sha256(sample_pdf()).hexdigest()
    storage.put.assert_called_once_with(attachment.storage_key, sample_pdf(), "application/pdf")
    assert client.get(f"/static/{attachment.storage_key}").status_code == 404

    download = client.get(scenario.attachment_url(attachment.id), headers=without_tab)
    assert download.status_code == 401
    storage.get.assert_not_called()


@pytest.mark.parametrize("header", [None, "invalid-uuid"])
def test_upload_requires_uuid_key(client, scenario, storage, header):
    headers = scenario.auth()
    if header is not None:
        headers["Idempotency-Key"] = header
    response = client.post(
        f"{scenario.revision_url()}/attachments",
        headers=headers,
        files={"file": ("report.pdf", sample_pdf(), "application/pdf")},
    )
    assert response.status_code == 422, response.text
    storage.put.assert_not_called()


@pytest.mark.parametrize(
    ("actor", "status"),
    [("other", 403), ("teacher", 403), ("outsider", 404), ("inactive", 401)],
)
def test_upload_denies_accounts_without_permission(client, scenario, storage, actor, status):
    response = upload(client, scenario, actor=actor)
    assert response.status_code == status, response.text
    assert attachments(scenario) == []
    storage.put.assert_not_called()


@pytest.mark.parametrize("kind", ["club", "year", "membership"])
def test_upload_requires_current_representative_of_active_club_and_year(
    client, scenario, storage, kind
):
    with SessionLocal.begin() as db:
        if kind == "club":
            db.get(Club, scenario.club_id).status = ClubStatus.ARCHIVED
        elif kind == "year":
            db.get(AcademicYear, scenario.year_id).status = AcademicYearStatus.CLOSED
        else:
            membership = db.scalar(select(ClubMembership).where(
                ClubMembership.account_id == scenario.accounts["student"],
                ClubMembership.club_id == scenario.club_id,
            ))
            membership.status = MembershipStatus.ENDED
            membership.end_date = date.today()

    response = upload(client, scenario)
    assert response.status_code == (403 if kind == "membership" else 409), response.text
    storage.put.assert_not_called()


@pytest.mark.parametrize("state", ["pending", "approved", "purged"])
def test_upload_requires_draft(client, scenario, storage, state):
    if state == "approved":
        publish(client, scenario)
    elif state == "purged":
        with SessionLocal.begin() as db:
            revision = db.get(ActivityRevision, scenario.revision_id)
            revision.title = None
            revision.activity_date = None
            revision.description = None
            revision.category = None
            revision.content_purged_at = datetime.now(timezone.utc)
    else:
        response = client.post(f"{scenario.revision_url()}/submit", headers=scenario.auth())
        assert response.status_code == 200

    response = upload(client, scenario)
    assert response.status_code == 409, response.text
    storage.put.assert_not_called()


@pytest.mark.parametrize("component", ["club_id", "activity_id", "revision_id"])
def test_upload_checks_nested_resource_path(client, scenario, storage, component):
    path = scenario.revision_url().replace(str(getattr(scenario, component)), str(uuid4()))
    response = client.post(
        f"{path}/attachments",
        headers={**scenario.auth(), "Idempotency-Key": str(uuid4())},
        files={"file": ("report.pdf", sample_pdf(), "application/pdf")},
    )
    assert response.status_code == 404, response.text
    storage.put.assert_not_called()


def test_upload_rejects_invalid_attachment_before_storage(client, scenario, storage):
    assert upload(client, scenario, name="../report.pdf").status_code == 400
    assert upload(client, scenario, content=b"%PDF-1.7\n").status_code == 400
    storage.put.assert_not_called()


@pytest.mark.parametrize("filename", [r"C:\private\report.pdf", r"\\server\share\report.pdf"])
def test_upload_rejects_windows_path_before_multipart_can_strip_it(
    client, scenario, storage, filename
):
    boundary = "clubos-test"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + sample_pdf() + f"\r\n--{boundary}--\r\n".encode()
    response = client.post(
        f"{scenario.revision_url()}/attachments",
        headers={
            **scenario.auth(),
            "Idempotency-Key": str(uuid4()),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        content=body,
    )
    assert response.status_code == 400, response.text
    storage.put.assert_not_called()


def test_upload_rejects_oversized_file_and_multipart_body(client, scenario, storage):
    boundary = "clubos-test"
    with_overhead = b"x" * (MAX_FILE_SIZE + 1)
    response = upload(client, scenario, content=with_overhead)
    assert response.status_code == 413, response.text

    def body():
        yield (
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"file\"; filename=\"report.pdf\"\r\n"
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        chunk = b"x" * (1024 * 1024)
        for _ in range(11):
            yield chunk
        yield f"\r\n--{boundary}--\r\n".encode()

    response = client.post(
        f"{scenario.revision_url()}/attachments",
        headers={
            **scenario.auth(),
            "Idempotency-Key": str(uuid4()),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        content=body(),
    )
    assert response.status_code == 413, response.text
    assert attachments(scenario) == []
    storage.put.assert_not_called()


def test_upload_replay_and_conflicts(client, scenario, storage):
    key = uuid4()
    first = upload(client, scenario, key=key)
    replay = upload(client, scenario, key=key)
    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()

    assert upload(client, scenario, key=key, name="renamed.pdf").status_code == 409
    assert upload(client, scenario, key=key, content=sample_pdf("Changed")).status_code == 409
    assert upload(client, scenario, key=key, content=b"malformed").status_code == 409
    assert len(attachments(scenario)) == 1
    storage.put.assert_called_once()
    storage.delete.assert_not_called()


@pytest.mark.parametrize("after_write", [False, True])
def test_upload_storage_failure_leaves_no_metadata_and_allows_retry(
    client, scenario, storage, memory_storage, after_write
):
    def fail_put(key, content, content_type):
        if after_write:
            memory_storage.put(key, content, content_type)
        raise StorageError("private-provider-error")

    key = uuid4()
    storage.put.side_effect = fail_put
    response = upload(client, scenario, key=key)
    assert response.status_code == 503, response.text
    assert "private-provider-error" not in response.text
    assert attachments(scenario) == []

    failed_key = storage.put.call_args.args[0]
    with pytest.raises(ObjectMissing):
        memory_storage.get(failed_key)

    storage.put.side_effect = None
    assert upload(client, scenario, key=key).status_code == 201
    attachment, = attachments(scenario)
    assert attachment.storage_key != failed_key
    with memory_storage.get(attachment.storage_key) as stored:
        assert stored.read() == sample_pdf()


def test_upload_database_constraint_cannot_replace_existing_object(
    client, scenario, storage, monkeypatch
):
    first = upload(client, scenario)
    assert first.status_code == 201
    attachment, = attachments(scenario)

    monkeypatch.setattr(activity_attachments, "uuid4", lambda: attachment.id)
    rejected = upload(client, scenario, content=sample_pdf("Different"))
    assert rejected.status_code == 503, rejected.text
    assert [item.id for item in attachments(scenario)] == [attachment.id]
    storage.put.assert_called_once()
    storage.delete.assert_not_called()
    with storage.get(attachment.storage_key) as stored:
        assert stored.read() == sample_pdf()


@pytest.mark.parametrize("commit_succeeded", [False, True])
def test_upload_reconciles_uncertain_database_commit(
    client, scenario, storage, monkeypatch, commit_succeeded
):
    original_commit = Session.commit

    def fail_commit(db):
        if storage.put.called:
            if commit_succeeded:
                original_commit(db)
            raise OperationalError("COMMIT", None, RuntimeError("private-database-error"))
        original_commit(db)

    key = uuid4()
    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_commit)
        result = upload(client, scenario, key=key)
    assert result.status_code == 503, result.text
    assert "private-database-error" not in result.text
    storage.put.assert_called_once()
    storage.delete.assert_not_called()
    uncertain_key = storage.put.call_args.args[0]

    retry = upload(client, scenario, key=key)
    assert retry.status_code == 201, retry.text
    attachment, = attachments(scenario)
    assert (attachment.storage_key == uncertain_key) is commit_succeeded
    assert storage.put.call_count == (1 if commit_succeeded else 2)

    with SessionLocal() as db:
        assert reconcile_orphaned_upload(db, storage, uncertain_key) is not commit_succeeded

    if not commit_succeeded:
        with pytest.raises(ObjectMissing):
            storage.get(uncertain_key)
    with storage.get(attachment.storage_key) as stored:
        assert stored.read() == sample_pdf()


def test_draft_download_enforces_roles_and_safe_headers(client, scenario, storage):
    filename = "résumé final.pdf"
    upload_response = upload(client, scenario, name=filename)
    assert upload_response.status_code == 201, upload_response.text
    path = scenario.attachment_url(upload_response.json()["id"])

    for actor in ("student", "teacher", "admin"):
        response = client.get(path, headers=scenario.auth(actor))
        assert response.status_code == 200, response.text
        assert response.content == sample_pdf()
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-type"] == "application/pdf"
        assert int(response.headers["content-length"]) == len(response.content)
        assert response.headers["content-disposition"].startswith("attachment")
        assert f"filename*=UTF-8''{quote(filename)}" in response.headers["content-disposition"]

    storage.get.reset_mock()
    for actor in ("other", "other_teacher", "outsider", "owner"):
        response = client.get(path, headers=scenario.auth(actor))
        expected = 403 if actor == "owner" else 404
        assert response.status_code == expected, response.text
    storage.get.assert_not_called()


def test_published_download_enforces_school_and_path(client, scenario, storage):
    uploaded = upload(client, scenario)
    assert uploaded.status_code == 201, uploaded.text
    attachment_id = uploaded.json()["id"]
    publish(client, scenario)
    path = scenario.attachment_url(attachment_id)

    for actor in ("other", "other_teacher", "admin"):
        assert client.get(path, headers=scenario.auth(actor)).status_code == 200

    storage.get.reset_mock()
    outsider_headers = scenario.auth("outsider")
    real = client.get(path, headers=outsider_headers)
    nonexistent = client.get(scenario.attachment_url(uuid4()), headers=outsider_headers)
    assert real.status_code == nonexistent.status_code == 404
    assert real.json() == nonexistent.json()
    assert client.get(
        scenario.attachment_url(attachment_id, uuid4()), headers=scenario.auth("other")
    ).status_code == 404
    assert client.get(path, headers=scenario.auth("owner")).status_code == 403
    storage.get.assert_not_called()


def test_download_reads_short_storage_chunks(client, scenario, storage):
    class ShortReader(BytesIO):
        def read(self, size: int = -1) -> bytes:
            assert size >= 0
            return super().read(min(size, 8))

    uploaded = upload(client, scenario)
    assert uploaded.status_code == 201, uploaded.text
    storage.get.return_value = ShortReader(sample_pdf())
    response = client.get(
        scenario.attachment_url(uploaded.json()["id"]), headers=scenario.auth()
    )
    assert response.status_code == 200
    assert response.content == sample_pdf()


def test_download_handles_missing_corrupt_and_unavailable_objects(client, scenario, storage):
    uploaded = upload(client, scenario)
    assert uploaded.status_code == 201, uploaded.text
    attachment, = attachments(scenario)
    path = scenario.attachment_url(attachment.id)
    storage.delete(attachment.storage_key)
    missing = client.get(path, headers=scenario.auth())
    assert missing.status_code == 404
    assert attachment.storage_key not in missing.text

    storage.put(attachment.storage_key, b"wrong size", "application/pdf")
    corrupt = client.get(path, headers=scenario.auth())
    assert corrupt.status_code == 503
    assert "wrong size" not in corrupt.text

    storage.get.side_effect = StorageError("private-provider-error")
    unavailable = client.get(path, headers=scenario.auth())
    assert unavailable.status_code == 503
    assert "private-provider-error" not in unavailable.text


def test_retention_keeps_recoverable_objects_then_retries_deletion(
    client, scenario, storage, memory_storage
):
    first = upload(client, scenario)
    second = upload(client, scenario, name="second.pdf")
    assert first.status_code == second.status_code == 201
    old_attachments = attachments(scenario)
    old_review_id = publish(client, scenario)

    replacement = client.post(
        f"{scenario.revision_url()}/replace",
        json={
            "title": "Replacement",
            "description": "Updated workshop",
            "activity_date": date.today().isoformat(),
            "category": "STEM",
        },
        headers={**scenario.auth(), "Idempotency-Key": str(uuid4())},
    )
    assert replacement.status_code == 201, replacement.text
    replacement_id = UUID(replacement.json()["revision_id"])
    new_upload = upload(client, scenario, revision_id=replacement_id)
    assert new_upload.status_code == 201, new_upload.text
    new_review_id = publish(client, scenario, replacement_id)
    new_attachment, = attachments(scenario, replacement_id)

    with SessionLocal() as db:
        old = db.get(ActivityRevision, scenario.revision_id)
        assert old.is_published is False
        assert old.superseded_at is not None
        day_29 = old.superseded_at + timedelta(days=29)
        day_30 = old.superseded_at + timedelta(days=30)
        assert purge_expired_superseded_content(db, storage=storage, now=day_29) == 0

    storage.delete.assert_not_called()
    for attachment in old_attachments:
        old_path = scenario.attachment_url(attachment.id)
        assert client.get(old_path, headers=scenario.auth("admin")).status_code == 200
        assert client.get(old_path, headers=scenario.auth("other")).status_code == 404

    attempts = []

    def fail_second_delete(key):
        attempts.append(key)
        if len(attempts) == 2:
            raise StorageError("private-provider-error")
        memory_storage.delete(key)

    storage.delete.side_effect = fail_second_delete
    with SessionLocal() as db:
        assert purge_expired_superseded_content(db, storage=storage, now=day_30) == 0
        old = db.get(ActivityRevision, scenario.revision_id)
        assert old.content_purged_at is None
        assert old.title == "Robotics Workshop"
        assert {item.id for item in attachments(scenario)} == {item.id for item in old_attachments}

    assert len(attempts) == 2
    storage.delete.side_effect = None
    with SessionLocal() as db:
        assert purge_expired_superseded_content(db, storage=storage, now=day_30) == 1
        old = db.get(ActivityRevision, scenario.revision_id)
        new = db.get(ActivityRevision, replacement_id)
        assert old.content_purged_at == day_30
        assert old.title is None
        assert new.content_purged_at is None
        assert new.is_published is True
        assert db.get(ActivityReview, old_review_id).revision_id == old.id
        assert db.get(ActivityReview, new_review_id).revision_id == new.id
        assert purge_expired_superseded_content(db, storage=storage, now=day_30) == 0

    assert attachments(scenario) == []
    for attachment in old_attachments:
        with pytest.raises(ObjectMissing):
            storage.get(attachment.storage_key)
        assert client.get(
            scenario.attachment_url(attachment.id), headers=scenario.auth("admin")
        ).status_code == 404

    assert client.get(
        scenario.attachment_url(new_attachment.id, replacement_id), headers=scenario.auth("other")
    ).status_code == 200


@pytest.mark.parametrize("conflict", [False, True])
def test_concurrent_uploads_make_one_object_and_metadata(scenario, storage, monkeypatch, conflict):
    ready = Barrier(2, timeout=10)
    backend_pids = Queue()

    def simultaneous_account(db: Session = Depends(get_db)):
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
        student = db.get(Account, scenario.accounts["student"])
        backend_pids.put(db.scalar(select(func.pg_backend_pid())))
        ready.wait()
        return student

    monkeypatch.setitem(app.dependency_overrides, require_account, simultaneous_account)
    key = uuid4()

    def post(content: bytes):
        with TestClient(app) as request_client:
            return upload(request_client, scenario, key=key, content=content)

    payloads = [sample_pdf(), sample_pdf("Changed") if conflict else sample_pdf()]
    with ThreadPoolExecutor(max_workers=2) as executor:
        requests = [executor.submit(post, content) for content in payloads]
        responses = [request.result(timeout=25) for request in requests]

    assert backend_pids.get_nowait() != backend_pids.get_nowait()
    assert sorted(response.status_code for response in responses) == [201, 409 if conflict else 201]
    attachment, = attachments(scenario)
    storage.put.assert_called_once()
    storage.delete.assert_not_called()

    for response in responses:
        if response.status_code == 201:
            assert UUID(response.json()["id"]) == attachment.id

    winner_index = next(i for i, response in enumerate(responses) if response.status_code == 201)
    with storage.get(attachment.storage_key) as stored:
        assert stored.read() == payloads[winner_index]


def test_orphan_reconciliation_waits_for_in_flight_upload(
    client, scenario, storage, memory_storage
):
    stored_keys = Queue()
    scanner_pids = Queue()
    upload_may_commit = Event()

    def put_then_wait(key, content, content_type):
        memory_storage.put(key, content, content_type)
        stored_keys.put(key)
        assert upload_may_commit.wait(timeout=10)

    def reconcile(key):
        with SessionLocal() as db:
            db.execute(text("SET LOCAL statement_timeout = '10s'"))
            scanner_pids.put(db.scalar(select(func.pg_backend_pid())))
            return reconcile_orphaned_upload(db, storage, key)

    storage.put.side_effect = put_then_wait
    with ThreadPoolExecutor(max_workers=2) as executor:
        upload_response = executor.submit(upload, client, scenario)
        try:
            key = stored_keys.get(timeout=10)
            scanner = executor.submit(reconcile, key)
            scanner_pid = scanner_pids.get(timeout=10)

            deadline = monotonic() + 5
            with SessionLocal() as db:
                while monotonic() < deadline:
                    blockers = db.scalar(
                        text("SELECT pg_blocking_pids(:pid)"), {"pid": scanner_pid}
                    )
                    if blockers:
                        break
                    sleep(0.01)
                else:
                    pytest.fail("Reconciliation did not wait for the upload's revision lock")
        finally:
            upload_may_commit.set()

        assert upload_response.result(timeout=10).status_code == 201
        assert scanner.result(timeout=10) is False

    attachment, = attachments(scenario)
    assert attachment.storage_key == key
    storage.delete.assert_not_called()
    with memory_storage.get(key) as stored:
        assert stored.read() == sample_pdf()


def test_retention_skips_revision_while_authorized_download_reads(
    client, scenario, storage, memory_storage
):
    uploaded = upload(client, scenario)
    assert uploaded.status_code == 201, uploaded.text
    attachment, = attachments(scenario)
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as db:
        revision = db.get(ActivityRevision, scenario.revision_id)
        revision.status = ActivityRevisionStatus.APPROVED
        revision.superseded_at = now - timedelta(days=30)

    started = Event()
    finish = Event()
    def wait_in_storage(key):
        started.set()
        assert finish.wait(timeout=10)
        return memory_storage.get(key)

    storage.get.side_effect = wait_in_storage
    path = scenario.attachment_url(attachment.id)
    with ThreadPoolExecutor(max_workers=1) as executor:
        response = executor.submit(client.get, path, headers=scenario.auth("admin"))
        try:
            assert started.wait(timeout=10)
            with SessionLocal() as db:
                assert purge_expired_superseded_content(db, storage=storage, now=now) == 0
            storage.delete.assert_not_called()
        finally:
            finish.set()
        assert response.result(timeout=10).content == sample_pdf()

    with SessionLocal() as db:
        assert purge_expired_superseded_content(db, storage=storage, now=now) == 1
    assert client.get(path, headers=scenario.auth("admin")).status_code == 404
