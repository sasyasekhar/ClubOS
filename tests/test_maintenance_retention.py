from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config.database import SessionLocal, engine
from app.maintenance import run_maintenance
from app.models.activity_review import ActivityReview
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
from app.models.auth_session import AuthSession
from app.models.club import Club
from app.models.enums import ActivityRevisionStatus, ClubStatus, ImpersonationStatus, LoginOutcome
from app.models.impersonation_session import ImpersonationSession
from app.models.login_history import LoginHistory
from app.models.maintenance_state import MaintenanceState
from app.models.operation_receipt import OperationReceipt
from app.services.activity_attachments import attachment_object_key
from app.services.history_retention import RETENTION_PERIOD, cleanup_history
from app.services.maintenance import MaintenanceError, bind_storage, exclusive_maintenance
from app.services.revision_retention import purge_superseded_batch
from app.services.sessions import database_now
from app.services.storage import StorageError
from app.services.storage.memory import MemoryAttachmentStorage
from test_academic_year_closure_workflow import add_activity, client, scenario
from test_academic_year_history import Publication, add_attachment
from test_admin_impersonation import bound_session, clean_impersonation_history, start
from test_impersonated_activity_reviews import review


def test_history_cutoff_preserves_active_sessions_and_review_provenance(client, scenario):
    with SessionLocal.begin() as db:
        club, activity, (revision,) = add_activity(db, scenario, ActivityRevisionStatus.PENDING)
    impersonation = start(client, scenario)
    session_id = UUID(impersonation.json()["impersonation_session_id"])
    target = Publication(club, activity, revision)
    assert review(client, scenario, target, session_id).status_code == 201

    with SessionLocal.begin() as db:
        now = database_now(db)
        cutoff = now - RETENTION_PERIOD
        db.execute(AuditLog.__table__.update().where(AuditLog.impersonation_session_id == session_id).values(occurred_at=cutoff))
        older, exact, younger = (uuid4() for _ in range(3))
        names = dict(older=older, exact=exact, younger=younger)
        for offset, identifier in ((-1, older), (0, exact), (1, younger)):
            db.add(LoginHistory(
                id=identifier, account_id=scenario.accounts["admin"], outcome=LoginOutcome.SUCCESS,
                occurred_at=cutoff + timedelta(microseconds=offset),
            ))
        unbound = bound_session(db, scenario, "admin_tab")
        unbound.expires_at = cutoff
        unbound_id = unbound.id
    try:
        with SessionLocal() as db:
            preview = cleanup_history(db, now=now, limit=500, dry_run=True)
            assert preview["login_history"] >= 2
            assert db.get(LoginHistory, names["exact"]) is not None
            db.rollback()
            removed = cleanup_history(db, now=now, limit=500)
            repeated = cleanup_history(db, now=now, limit=500)
            assert removed["login_history"] >= 2 and repeated == {key: 0 for key in repeated}
            assert db.get(LoginHistory, older) is None and db.get(LoginHistory, exact) is None
            assert db.get(LoginHistory, younger) is not None
            assert db.get(AuthSession, unbound_id) is None
            assert bound_session(db, scenario) is not None
            stored = db.get(ImpersonationSession, session_id)
            assert stored.status == ImpersonationStatus.ACTIVE
            assert db.scalar(select(ActivityReview.impersonation_session_id).where(ActivityReview.revision_id == revision)) == session_id
            assert db.scalar(select(AuditLog.id).where(AuditLog.impersonation_session_id == session_id)) is None
        assert client.get("/admin/impersonation/current", headers=scenario.auth("admin")).json()["active"] is True
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(LoginHistory).where(LoginHistory.id.in_((older, exact, younger))))


def test_old_resubmission_retry_survives_audit_cleanup(client, scenario):
    with SessionLocal.begin() as db:
        club, _, _ = add_activity(db, scenario, club_status=ClubStatus.CHANGES_REQUESTED)
    path = f"/clubs/{club}/resubmit"
    key = uuid4()
    headers = {**scenario.auth("student"), "Idempotency-Key": str(key)}
    data = {"name": "Updated robotics", "description": "Private draft text", "category": "STEM",
            "teacher_account_id": str(scenario.accounts["teacher"])}
    created = client.post(path, headers=headers, json=data)
    assert created.status_code == 200, created.text
    with SessionLocal.begin() as db:
        now = database_now(db)
        event = db.scalar(select(AuditLog).where(AuditLog.entity_id == str(club), AuditLog.action == "CLUB_RESUBMITTED"))
        operation_id = event.id
        event.occurred_at = now - RETENTION_PERIOD
        assert event.details is None
        # A later club review could make this old operation otherwise eligible again.
        current = db.get(Club, club)
        current.name = "Later club name"
        current.status = ClubStatus.CHANGES_REQUESTED
    with SessionLocal() as db:
        cleanup_history(db, now=now, limit=500)
    replay = client.post(path, headers=headers, json=data)
    assert replay.status_code == 200 and replay.json() == created.json()
    assert client.post(path, headers=headers, json={**data, "description": "Changed request"}).status_code == 409
    with SessionLocal.begin() as db:
        assert db.get(Club, club).name == "Later club name"
        assert db.get(AuditLog, operation_id) is None
        receipt = db.get(OperationReceipt, operation_id)
        assert len(receipt.fingerprint) == 32 and receipt.result_id is None
        db.delete(receipt)


def test_concurrent_history_workers_take_distinct_batches(scenario):
    with SessionLocal.begin() as db:
        now = database_now(db)
        ids = [uuid4(), uuid4()]
        db.add_all(AuditLog(id=identifier, actor_account_id=scenario.accounts["admin"], action="TEST_RETENTION",
                            entity_type="fixture", occurred_at=now - timedelta(days=365)) for identifier in ids)
    barrier = Barrier(2, timeout=5)

    def run():
        with engine.connect() as connection, Session(bind=connection) as db:
            pid = db.scalar(select(func.pg_backend_pid()))
            db.commit()
            barrier.wait()
            return pid, cleanup_history(db, now=now, limit=1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _: run(), range(2)))
    assert first[0] != second[0]
    assert first[1]["audit_logs"] == second[1]["audit_logs"] == 1
    with SessionLocal() as db:
        assert db.scalars(select(AuditLog.id).where(AuditLog.id.in_(ids))).all() == []


def test_a_failed_revision_does_not_starve_the_next_batch(scenario, monkeypatch):
    storage = MemoryAttachmentStorage()
    with SessionLocal.begin() as db:
        now = database_now(db)
        club, activity, (first, second, current) = add_activity(
            db, scenario, ActivityRevisionStatus.DRAFT, ActivityRevisionStatus.DRAFT, ActivityRevisionStatus.APPROVED,
        )
        db.get(ActivityRevision, first).superseded_at = now - timedelta(days=35)
        db.get(ActivityRevision, second).superseded_at = now - timedelta(days=31)
        path = add_attachment(db, storage, Publication(club, activity, first))
    failed_key = attachment_object_key(first, UUID(path.rsplit("/", 1)[1]))
    real_delete = storage.delete

    def delete_object(key):
        if key == failed_key:
            raise StorageError("Fixture storage failure")
        real_delete(key)

    monkeypatch.setattr(storage, "delete", delete_object)
    with SessionLocal() as db:
        first_pass = purge_superseded_batch(db, storage=storage, now=now, limit=1)
        second_pass = purge_superseded_batch(db, storage=storage, now=now, limit=1)
        assert (first_pass.purged, first_pass.failed, second_pass.purged) == (0, 1, 1)
        assert db.get(ActivityRevision, second).content_purged_at == now
        assert db.get(ActivityRevision, current).is_published
        assert db.get(ActivityRevision, first).content_purged_at is None
        db.rollback()
        monkeypatch.setattr(storage, "delete", real_delete)
        assert purge_superseded_batch(db, storage=storage, now=now, limit=1).purged == 1


def test_maintenance_checks_storage_pairing_and_serializes_runs():
    storage = MemoryAttachmentStorage()
    try:
        with exclusive_maintenance() as db:
            bind_storage(db, storage, "test")
            key = attachment_object_key(uuid4(), uuid4())
            storage.put(key, b"unreferenced fixture", "application/pdf")
            assert run_maintenance(db, storage, "test", 5, True)["unreferenced_attachment_keys"] == 1
            assert list(storage.iter_keys("activity-attachments")) == [key]
            with pytest.raises(MaintenanceError, match="database lock"), exclusive_maintenance():
                pytest.fail("A second worker acquired the maintenance lock")
            with pytest.raises(MaintenanceError, match="not paired"):
                run_maintenance(db, MemoryAttachmentStorage(), "test", 5, False)
            with pytest.raises(MaintenanceError, match="test isolation"):
                run_maintenance(db, storage, "development", 5, False)
            assert run_maintenance(db, storage, "test", 5, False)["orphan_objects_removed"] == 1
            assert list(storage.iter_keys("activity-attachments")) == []
    finally:
        with SessionLocal.begin() as db:
            state = db.get(MaintenanceState, 1)
            state.storage_environment = None
            state.orphan_cursor = None
