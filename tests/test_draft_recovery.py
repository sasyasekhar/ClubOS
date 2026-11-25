from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.config.database import SessionLocal
from app.models.academic_year import AcademicYear
from app.models.account import Account
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
from app.models.deleted_record import DeletedRecord
from app.models.enums import AcademicYearStatus, ActivityRevisionStatus, DeletionStatus
from app.services.activity_drafts import ActivityError
from app.services.draft_recovery import complete_draft_recovery, expire_draft_recovery, stage_draft_recovery
from test_academic_year_closure_workflow import add_activity, scenario
from test_admin_impersonation import clock


@pytest.fixture
def recovery(scenario):
    with SessionLocal.begin() as db:
        club, _, (revision,) = add_activity(db, scenario, ActivityRevisionStatus.DRAFT)
        actor = db.get(Account, scenario.accounts["student"])
        key = uuid4()
        record = stage_draft_recovery(db, actor, club, revision, key)
        db.flush()
        assert stage_draft_recovery(db, actor, club, revision, key).id == record.id
        data = club, revision, record.id, record.expires_at
    try:
        yield data
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(DeletedRecord).where(DeletedRecord.id == data[2]))


def test_recovery_ownership_and_actual_actor_are_preserved(scenario, recovery):
    club, revision, record, expires = recovery
    with SessionLocal() as db:
        row = db.get(DeletedRecord, record)
        assert row.school_id == scenario.schools["own"] and row.revision_id == revision
        assert row.deleted_by == scenario.accounts["student"]
        assert expires == row.deleted_at + timedelta(days=30)
        wrong_school = db.get(Account, scenario.accounts["other_admin"])
        with pytest.raises(ActivityError) as denied:
            complete_draft_recovery(db, wrong_school, club, record, uuid4())
        assert denied.value.status_code == 404


def test_recovery_at_cutoff_is_denied_and_expires_once(scenario, clock, recovery):
    club, revision, record, expires = recovery
    clock.now = expires
    with SessionLocal() as db:
        with pytest.raises(ActivityError, match="no longer recoverable"):
            complete_draft_recovery(db, db.get(Account, scenario.accounts["admin"]), club, record, uuid4())
        db.rollback()
        assert expire_draft_recovery(db, limit=100, dry_run=True) == 1
        assert expire_draft_recovery(db, limit=100) == 1
        assert expire_draft_recovery(db, limit=100) == 0
        expired = db.get(DeletedRecord, record)
        assert expired.status == DeletionStatus.PERMANENTLY_DELETED and expired.revision_id is None
        assert db.get(ActivityRevision, revision).content_purged_at is None


@pytest.mark.parametrize("failure", ["purged", "closed", "published"])
def test_recovery_checks_retained_draft_and_current_write_authority(scenario, recovery, failure):
    club, revision, record, _ = recovery
    with SessionLocal.begin() as db:
        if failure == "closed":
            db.get(AcademicYear, scenario.years["active"]).status = AcademicYearStatus.CLOSED
        else:
            target = db.get(ActivityRevision, revision)
            if failure == "purged":
                target.title = target.description = target.category = target.activity_date = None
                target.content_purged_at = func.clock_timestamp()
            else:
                target.status = ActivityRevisionStatus.APPROVED
                target.is_published = True
    with SessionLocal() as db, pytest.raises(ActivityError) as denied:
        complete_draft_recovery(db, db.get(Account, scenario.accounts["admin"]), club, record, uuid4())
    assert denied.value.status_code == 409


def test_concurrent_recovery_retry_records_one_transition(scenario, recovery):
    club, revision, record, _ = recovery
    key = uuid4()
    barrier = Barrier(2, timeout=5)

    def restore():
        with SessionLocal.begin() as db:
            pid = db.scalar(select(func.pg_backend_pid()))
            actor = db.get(Account, scenario.accounts["admin"])
            barrier.wait()
            row = complete_draft_recovery(db, actor, club, record, key)
            return pid, row.id, row.restored_at

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _: restore(), range(2)))
    assert first[0] != second[0] and first[1:] == second[1:]
    with SessionLocal() as db:
        row = db.get(DeletedRecord, record)
        assert row.restored_by == scenario.accounts["admin"] and row.status == DeletionStatus.RESTORED
        assert not db.get(ActivityRevision, revision).is_published
        event, = db.scalars(select(AuditLog).where(
            AuditLog.entity_id == str(record), AuditLog.action == "DRAFT_RECOVERY_COMPLETED",
        )).all()
        assert event.actor_account_id == row.restored_by
