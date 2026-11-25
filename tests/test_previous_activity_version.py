from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from queue import Queue
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config.database import SessionLocal
from app.main import app
from app.models.academic_year import AcademicYear
from app.models.account import Account
from app.models.activity_attachment import ActivityAttachment
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.enums import AcademicYearStatus, ActivityRevisionStatus as Status, ClubRole, MembershipStatus
from app.models.operation_receipt import OperationReceipt
from app.services.activity_attachments import reconcile_orphaned_upload
from app.services.activity_recovery import RESTORED, restore_previous_version
from app.services.history_retention import RETENTION_PERIOD, cleanup_history
from app.services.revision_retention import purge_expired_superseded_content
from app.services.storage import StorageError, get_attachment_storage
from app.services.storage.memory import MemoryAttachmentStorage
from attachment_samples import sample_pdf
from test_academic_year_closure_workflow import add_activity, backend_pid, client, scenario, set_request_deadline
from test_academic_year_history import Publication, add_archived_year, add_attachment, add_publication
from test_academic_year_write_guard import wait_for_blocking
from test_admin_impersonation import clean_impersonation_history, clock, ordered_requests, start
from test_impersonated_activity_reviews import review, review_waiting_on_transaction, revision_url


@dataclass(frozen=True)
class Versions:
    older: Publication
    previous: Publication
    current: Publication
    never_published: Publication
    unpublished_approval: Publication

    @property
    def path(self):
        return f"/clubs/{self.current.club_id}/activities/{self.current.activity_id}"


@pytest.fixture
def versions(client, scenario, clock):
    with SessionLocal.begin() as db:
        club, activity, revisions = add_activity(db, scenario, Status.PENDING, Status.PENDING, Status.PENDING, Status.REJECTED, Status.DRAFT)
    older, previous, current, rejected, unpublished = (Publication(club, activity, revision) for revision in revisions)
    for item in (older, previous, current):
        clock.now += timedelta(seconds=1)
        result = review(client, scenario, item, None, actor="teacher")
        assert result.status_code == 201, result.text
    with SessionLocal.begin() as db:
        db.get(ActivityRevision, rejected.revision_id).superseded_at = clock.now + timedelta(seconds=1)
        db.get(ActivityRevision, unpublished.revision_id).status = Status.APPROVED
    return Versions(older, previous, current, rejected, unpublished)


@pytest.fixture
def storage(monkeypatch):
    storage = MemoryAttachmentStorage()
    monkeypatch.setitem(app.dependency_overrides, get_attachment_storage, lambda: storage)
    return storage


def preview(c, scenario, versions, actor="student"):
    return c.get(f"{versions.path}/previous-version", headers=scenario.auth(actor))


def restore(c, scenario, versions, *, key=None, expected=None, actor="student"):
    return c.post(f"{versions.path}/restore-previous-version", headers={
        **scenario.auth(actor), "Idempotency-Key": str(key or uuid4()),
    }, json={"expected_revision_id": str(expected or versions.previous.revision_id)})


def publication(c, scenario):
    items = c.get("/activities/published", headers=scenario.auth("student")).json()
    revision, = items
    return revision["revision_id"]


def assert_no_restore(db, versions):
    assert db.scalar(select(AuditLog.id).where(
        AuditLog.action == RESTORED, AuditLog.details["source_revision_id"].astext == str(versions.previous.revision_id),
    )) is None
    assert len(db.scalars(select(ActivityRevision.id).where(
        ActivityRevision.activity_id == versions.current.activity_id,
    )).all()) == 5


def test_preview_only_exposes_the_immediate_previous_publication(client, scenario, versions, storage):
    with SessionLocal.begin() as db:
        previous_url = add_attachment(db, storage, versions.previous)
        older_url = add_attachment(db, storage, versions.older)
        # Give the oldest publication a higher revision number to prove timestamp ordering.
        db.get(ActivityRevision, versions.older.revision_id).revision_number = 20
    response = preview(client, scenario, versions)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    data = response.json()
    assert data["revision_id"] == str(versions.previous.revision_id)
    assert data["current_revision_id"] == str(versions.current.revision_id)
    assert data["published_at"] and data["superseded_at"] and data["recovery_expires_at"]
    assert data["title"] == "Revision 2"
    assert data["attachments"][0]["file_name"] == "report.pdf" and "storage_key" not in response.text
    for item in (versions.older, versions.never_published, versions.unpublished_approval):
        assert str(item.revision_id) not in response.text
    headers = scenario.auth("student")
    assert client.get(previous_url, headers=headers).content == sample_pdf()
    assert client.get(older_url, headers=headers).status_code == 404
    for actor in ("admin", "teacher"):
        assert client.get(older_url, headers=scenario.auth(actor)).content == sample_pdf()
    dashboard = client.get("/dashboard", headers=headers).json()["activity_revisions"]["items"]
    assert str(versions.older.revision_id) not in {item["revision_id"] for item in dashboard}
    assert client.get(revision_url(versions.older), headers=headers).status_code == 404


def test_no_previous_publication_when_only_current_and_rejected_exist(client, scenario, versions):
    with SessionLocal.begin() as db:
        for publication in (versions.older, versions.previous):
            db.get(ActivityRevision, publication.revision_id).superseded_at = None
    assert preview(client, scenario, versions).json()["detail"]["code"] == "ACTIVITY_NO_PREVIOUS_VERSION"
    assert restore(client, scenario, versions).status_code == 404


@pytest.mark.parametrize("offset,expected", [(-1, 201), (0, 410), (1, 410)])
def test_recovery_is_checked_at_the_exact_cutoff(client, scenario, versions, clock, storage, offset, expected):
    with SessionLocal.begin() as db:
        source = db.get(ActivityRevision, versions.previous.revision_id)
        source.superseded_at = clock.now - RETENTION_PERIOD
        db.get(ActivityRevision, versions.older.revision_id).superseded_at = source.superseded_at - timedelta(days=1)
        url = add_attachment(db, storage, versions.previous)
    clock.now += timedelta(microseconds=offset)
    response = preview(client, scenario, versions)
    assert response.status_code == (200 if expected == 201 else 410)
    assert client.get(url, headers=scenario.auth("student")).status_code == (200 if expected == 201 else 404)
    result = restore(client, scenario, versions)
    assert result.status_code == expected, result.text
    if expected == 410:
        assert result.json()["detail"]["code"] == "ACTIVITY_RECOVERY_EXPIRED"


def test_purged_previous_does_not_fall_back_to_older_content(client, scenario, versions, clock):
    with SessionLocal.begin() as db:
        source = db.get(ActivityRevision, versions.previous.revision_id)
        source.title = source.description = source.category = source.activity_date = None
        source.content_purged_at = clock.now
    assert preview(client, scenario, versions).json()["detail"]["code"] == "ACTIVITY_PREVIOUS_VERSION_UNAVAILABLE"
    assert restore(client, scenario, versions).status_code == 410


@pytest.mark.parametrize("decision", ["APPROVED", "REJECTED", "CHANGES_REQUESTED"])
def test_restored_copy_goes_through_normal_edit_submit_and_review(client, scenario, versions, decision):
    with SessionLocal() as db:
        original = db.get(ActivityRevision, versions.previous.revision_id)
        source_cutoff = original.superseded_at
    result = restore(client, scenario, versions)
    assert result.status_code == 201, result.text
    body = result.json()
    assert body["status"] == "DRAFT" and body["revision_number"] == 6
    draft = Publication(versions.current.club_id, versions.current.activity_id, UUID(body["revision_id"]))
    assert draft.revision_id != versions.previous.revision_id
    assert publication(client, scenario) == str(versions.current.revision_id)
    headers = scenario.auth("student")
    details = client.get(revision_url(draft), headers=headers).json()
    assert details["title"] == "Revision 2" and details["description"] == "Activity record" and not details["is_published"]
    edit = {"title": "Recovered workshop", "description": details["description"], "category": details["category"],
            "activity_date": details["activity_date"]}
    assert client.put(revision_url(draft), headers=headers, json=edit).status_code == 200
    assert client.post(revision_url(draft) + "/submit", headers=headers).status_code == 200
    assert publication(client, scenario) == str(versions.current.revision_id)
    assert review(client, scenario, draft, None, actor="teacher", decision=decision, comments="Reviewed.").status_code == 201
    assert publication(client, scenario) == str(draft.revision_id if decision == "APPROVED" else versions.current.revision_id)
    with SessionLocal() as db:
        old = db.get(ActivityRevision, versions.previous.revision_id)
        assert old.title == "Revision 2" and old.superseded_at == source_cutoff and not old.is_published
        audit, = db.scalars(select(AuditLog).where(AuditLog.action == RESTORED, AuditLog.entity_id == str(draft.revision_id))).all()
        assert audit.actor_account_id == scenario.accounts["student"] and audit.details == {"source_revision_id": str(old.id)}


def test_restore_receipt_survives_audit_retention(client, scenario, versions, clock):
    key = uuid4()
    created = restore(client, scenario, versions, key=key)
    assert created.status_code == 201, created.text
    with SessionLocal.begin() as db:
        event = db.scalar(select(AuditLog).where(AuditLog.action == RESTORED, AuditLog.entity_id == created.json()["revision_id"]))
        event.occurred_at = clock.now - RETENTION_PERIOD
        operation_id = event.id
    with SessionLocal() as db:
        cleanup_history(db, limit=500, now=clock.now)
        assert db.get(AuditLog, operation_id) is None
        assert db.get(OperationReceipt, operation_id).result_id == UUID(created.json()["revision_id"])
    assert restore(client, scenario, versions, key=key).json() == created.json()
    assert restore(client, scenario, versions, key=key, expected=versions.older.revision_id).status_code == 409


@pytest.mark.parametrize("status", [Status.DRAFT, Status.PENDING, Status.CHANGES_REQUESTED])
def test_unfinished_work_blocks_restoration(client, scenario, versions, status):
    with SessionLocal.begin() as db:
        row = db.get(ActivityRevision, versions.unpublished_approval.revision_id)
        row.status = status
    denied = restore(client, scenario, versions)
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "ACTIVITY_UNFINISHED_REVISION"
    with SessionLocal() as db:
        assert_no_restore(db, versions)
        assert db.get(ActivityRevision, versions.unpublished_approval.revision_id).status == status


def test_authorization_and_expected_revision(client, scenario, versions):
    for actor, status in ((None, 401), ("teacher", 403), ("admin", 403), ("owner", 403)):
        assert preview(client, scenario, versions, actor).status_code == status
        assert restore(client, scenario, versions, actor=actor).status_code == status
    denied = restore(client, scenario, versions, expected=versions.older.revision_id)
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "ACTIVITY_PREVIOUS_VERSION_CHANGED"
    with SessionLocal.begin() as db:
        outside = add_publication(db, scenario.years["other"], scenario.accounts["other_teacher"], "Foreign activity")
        membership = db.scalar(select(ClubMembership).where(
            ClubMembership.club_id == versions.current.club_id, ClubMembership.role == ClubRole.STUDENT_REP,
        ))
        membership.status = MembershipStatus.ENDED
    foreign = Versions(outside, outside, outside, outside, outside)
    assert preview(client, scenario, foreign).status_code == 404
    assert restore(client, scenario, foreign).status_code == 404
    assert preview(client, scenario, versions).status_code == 403
    assert restore(client, scenario, versions).status_code == 403


def test_impersonation_cannot_preview_or_restore_a_previous_version(client, scenario, versions):
    assert start(client, scenario, target="student").status_code == 201
    assert preview(client, scenario, versions, "admin").status_code == 403
    assert restore(client, scenario, versions, actor="admin").status_code == 403


@pytest.mark.parametrize("state", ["closed", "archived", "deadline"])
def test_restoration_obeys_academic_year_guards(client, scenario, versions, state):
    if state == "deadline":
        assert scenario.designate(client).status_code == 200
        request = scenario.request(client)
        assert request.status_code == 201
        set_request_deadline(request.json()["id"])
    else:
        with SessionLocal.begin() as db:
            year = add_archived_year(db, scenario.schools["own"]) if state == "archived" else scenario.years["historical"]
            db.get(Club, versions.current.club_id).academic_year_id = year
    assert restore(client, scenario, versions).status_code == 409


def test_restored_files_are_independent_after_cleanup(client, scenario, versions, storage):
    with SessionLocal.begin() as db:
        original_url = add_attachment(db, storage, versions.previous)
    restored = restore(client, scenario, versions)
    assert restored.status_code == 201, restored.text
    draft_id = UUID(restored.json()["revision_id"])
    with SessionLocal() as db:
        original = db.get(ActivityAttachment, UUID(original_url.rsplit("/", 1)[1]))
        copied, = db.scalars(select(ActivityAttachment).where(ActivityAttachment.revision_id == draft_id)).all()
        assert copied.storage_key != original.storage_key and copied.id != original.id
        assert copied.content_sha256 == original.content_sha256 and copied.scan_passed_at == original.scan_passed_at
        copied_id = copied.id
        source = db.get(ActivityRevision, versions.previous.revision_id)
        purge_expired_superseded_content(db, storage=storage, now=source.superseded_at + RETENTION_PERIOD)
    headers = scenario.auth("student")
    draft = Publication(versions.current.club_id, versions.current.activity_id, draft_id)
    assert client.get(draft.attachment_url(copied_id), headers=headers).content == sample_pdf()
    assert client.get(original_url, headers=headers).status_code == 404


@pytest.mark.parametrize("failure,expected", [("put", 503), ("missing", 503), ("unscanned", 409)])
def test_partial_file_failure_rolls_back_the_whole_restore(client, scenario, versions, storage, monkeypatch, failure, expected):
    with SessionLocal.begin() as db:
        add_attachment(db, storage, versions.previous)
        url = add_attachment(db, storage, versions.previous)
        db.flush()
        object_row = db.get(ActivityAttachment, UUID(url.rsplit("/", 1)[1]))
        if failure == "missing":
            storage.delete(object_row.storage_key)
        if failure == "unscanned":
            object_row.scan_passed_at = None
    if failure == "put":
        original_put = storage.put
        attempted = []

        def fail_second(key, content, media_type):
            attempted.append(key)
            original_put(key, content, media_type)
            if len(attempted) == 2:
                raise StorageError("fixture write failed after storing bytes")

        monkeypatch.setattr(storage, "put", fail_second)
    keys_before = storage.iter_keys("activity-attachments")
    response = restore(client, scenario, versions)
    assert response.status_code == expected, response.text
    assert storage.iter_keys("activity-attachments") == keys_before
    with SessionLocal() as db:
        assert_no_restore(db, versions)


def test_restore_rechecks_cutoff_after_copying_a_file(client, scenario, versions, clock, storage, monkeypatch):
    with SessionLocal.begin() as db:
        source = db.get(ActivityRevision, versions.previous.revision_id)
        source.superseded_at = clock.now - RETENTION_PERIOD + timedelta(microseconds=1)
        db.get(ActivityRevision, versions.older.revision_id).superseded_at = source.superseded_at - timedelta(days=1)
        add_attachment(db, storage, versions.previous)
    keys = storage.iter_keys("activity-attachments")
    original_put = storage.put

    def cross_cutoff(key, content, media_type):
        original_put(key, content, media_type)
        clock.now += timedelta(microseconds=1)

    monkeypatch.setattr(storage, "put", cross_cutoff)
    result = restore(client, scenario, versions)
    assert result.status_code == 410 and result.json()["detail"]["code"] == "ACTIVITY_RECOVERY_EXPIRED"
    assert storage.iter_keys("activity-attachments") == keys
    with SessionLocal() as db:
        assert_no_restore(db, versions)


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_restoration_produces_one_draft(scenario, versions, monkeypatch, storage, same_key):
    key = uuid4()
    second_key = key if same_key else uuid4()
    first, second = ordered_requests(monkeypatch, lambda c: restore(c, scenario, versions, key=key),
                                     lambda c: restore(c, scenario, versions, key=second_key))
    assert (first.status_code, second.status_code) == (201, 201 if same_key else 409)
    if same_key:
        assert second.json() == first.json()
    with SessionLocal() as db:
        drafts = db.scalars(select(ActivityRevision.id).where(
            ActivityRevision.activity_id == versions.current.activity_id, ActivityRevision.status == Status.DRAFT,
        )).all()
        assert drafts == [UUID(first.json()["revision_id"])]


def test_replacement_winning_a_race_makes_the_restore_request_stale(scenario, versions, monkeypatch):
    candidate = versions.unpublished_approval
    with SessionLocal.begin() as db:
        db.get(ActivityRevision, candidate.revision_id).status = Status.PENDING
    published, denied = ordered_requests(monkeypatch,
        lambda c: review(c, scenario, candidate, None, actor="teacher"),
        lambda c: restore(c, scenario, versions),
    )
    assert published.status_code == 201 and denied.status_code == 409
    assert denied.json()["detail"]["code"] == "ACTIVITY_PREVIOUS_VERSION_CHANGED"


@pytest.mark.parametrize("committed", [False, True])
def test_uncertain_commit_is_safe_to_reconcile_and_retry(client, scenario, versions, storage, monkeypatch, committed):
    with SessionLocal.begin() as db:
        add_attachment(db, storage, versions.previous)
    original_keys = set(storage.iter_keys("activity-attachments"))
    real_commit = Session.commit

    def lost_acknowledgement(db):
        if committed:
            real_commit(db)
        raise OperationalError("COMMIT", None, RuntimeError("fixture connection error"))

    key = uuid4()
    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", lost_acknowledgement)
        failed = restore(client, scenario, versions, key=key)
        assert failed.status_code == 503, failed.text
    attempted, = set(storage.iter_keys("activity-attachments")) - original_keys
    with SessionLocal() as db:
        assert reconcile_orphaned_upload(db, storage, attempted) is not committed
    retried = restore(client, scenario, versions, key=key)
    assert retried.status_code == 201, retried.text
    assert len(set(storage.iter_keys("activity-attachments")) - original_keys) == 1


def test_orphan_worker_waits_for_a_new_restored_revision(client, scenario, versions, storage, monkeypatch):
    with SessionLocal.begin() as db:
        add_attachment(db, storage, versions.previous)
    restoring, checking = Queue(), Queue()
    release = Event()
    original_put = storage.put

    def pause_copy(key, content, media_type):
        original_put(key, content, media_type)
        restoring.put(key)
        assert release.wait(timeout=8)

    def recover():
        with SessionLocal() as db:
            restoring.put(backend_pid(db))
            return restore_previous_version(db, storage, db.get(Account, scenario.accounts["student"]),
                versions.current.club_id, versions.current.activity_id, versions.previous.revision_id, uuid4())

    def reconcile(key):
        with SessionLocal() as db:
            checking.put(backend_pid(db))
            return reconcile_orphaned_upload(db, storage, key)

    monkeypatch.setattr(storage, "put", pause_copy)
    with ThreadPoolExecutor(max_workers=2) as executor:
        result = executor.submit(recover)
        try:
            holder = restoring.get(timeout=8)
            target = restoring.get(timeout=8)
            orphan = executor.submit(reconcile, target)
            waiter = checking.get(timeout=8)
            assert holder != waiter
            wait_for_blocking(waiter, holder)
            assert not orphan.done()
        finally:
            release.set()
        assert result.result(timeout=8)["status"] == Status.DRAFT
        assert orphan.result(timeout=8) is False
    with storage.get(target) as content:
        assert content.read() == sample_pdf()


@pytest.mark.parametrize("competing", ["retention", "closure"])
def test_waiting_restore_rechecks_after_an_independent_transaction(scenario, versions, clock, monkeypatch, storage, competing):
    cutoff = clock.now
    if competing == "retention":
        with SessionLocal.begin() as db:
            source = db.get(ActivityRevision, versions.previous.revision_id)
            source.superseded_at = cutoff - RETENTION_PERIOD
            db.get(ActivityRevision, versions.older.revision_id).superseded_at = source.superseded_at - timedelta(days=1)
            add_attachment(db, storage, versions.previous)
        clock.now = cutoff - timedelta(microseconds=1)

    def hold_first(db):
        if competing == "retention":
            return purge_expired_superseded_content(db, storage=storage, now=cutoff)
        db.get(AcademicYear, scenario.years["active"], with_for_update=True).status = AcademicYearStatus.CLOSED
        db.commit()

    def advance():
        clock.now = cutoff

    denied, count = review_waiting_on_transaction(
        monkeypatch, hold_first, lambda c: restore(c, scenario, versions), advance,
    )
    assert denied.status_code == (410 if competing == "retention" else 409), denied.text
    if competing == "retention":
        assert count == 2
    with SessionLocal() as db:
        assert_no_restore(db, versions)
