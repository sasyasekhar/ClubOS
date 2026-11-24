from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config.database import SessionLocal
from app.main import app
from app.models.academic_year import AcademicYear
from app.models.academic_year_final_export import AcademicYearFinalExport
from app.models.audit_log import AuditLog
from app.models.enums import AcademicYearStatus
from app.services import academic_year_exports as exports
from app.services.academic_year_archiving import ARCHIVE_ACTION
from test_academic_year_closure_workflow import concurrent_requests
from test_academic_year_exports import client, expire_export_limit, populate_year, read_export, scenario


def archive(client, scenario, year="closed", *, actor="admin", key=None, saved=True):
    return client.post(f"/academic-years/{scenario.years[year]}/archive", headers={
        **(scenario.auth(actor) if actor is not None else {}),
        "Idempotency-Key": str(key or uuid4()),
    }, json={"export_saved": saved})


def archive_audits(scenario, year="closed"):
    with SessionLocal() as db:
        return list(db.scalars(select(AuditLog).where(
            AuditLog.action == ARCHIVE_ACTION, AuditLog.entity_id == str(scenario.years[year]),
        )))


def final_export(scenario, year="closed"):
    with SessionLocal() as db:
        return db.get(AcademicYearFinalExport, scenario.years[year])


def close_test_year(scenario, year="active"):
    with SessionLocal.begin() as db:
        db.execute(update(AcademicYear).where(AcademicYear.id == scenario.years[year]).values(
            status=AcademicYearStatus.CLOSED,
        ))
        return db.scalar(select(func.clock_timestamp()))


def test_archive_preserves_records_and_uses_durable_export_evidence(client, scenario):
    with SessionLocal.begin() as db:
        populate_year(db, scenario)

    before, manifest = read_export(scenario.download(client))
    receipt = final_export(scenario)
    assert receipt.school_id == scenario.schools["own"]
    assert receipt.exported_by_account_id == scenario.accounts["admin"]
    assert receipt.generated_at == datetime.fromisoformat(manifest["generated_at"])

    # Export evidence remains usable independently of the export audit log.
    with SessionLocal.begin() as db:
        db.execute(delete(AuditLog).where(AuditLog.id == receipt.export_id, AuditLog.action == exports.EXPORT_ACTION))

    key = uuid4()
    result = archive(client, scenario, key=key)
    assert result.status_code == 200, result.text
    archived = result.json()
    assert archived["status"] == "ARCHIVED"
    assert archived["archived_by_account_id"] == str(scenario.accounts["admin"])
    assert archived["final_export_id"] == str(receipt.export_id)
    assert datetime.fromisoformat(archived["archived_at"]) >= receipt.generated_at
    assert archive(client, scenario, actor="second_tab", key=key).json() == archived
    assert archive(client, scenario, key=key, saved=False).status_code == 422
    assert archive(client, scenario).status_code == 409
    audit, = archive_audits(scenario)
    assert audit.details["export_saved"] is True
    assert audit.details["final_export_id"] == str(receipt.export_id)

    expire_export_limit(scenario)
    after, _ = read_export(scenario.download(client))
    assert after.pop("academic_year.csv")[0]["status"] == "ARCHIVED"
    assert before.pop("academic_year.csv")[0]["status"] == "CLOSED"
    assert after == before
    assert final_export(scenario).export_id == receipt.export_id


def test_active_exports_never_create_final_evidence(client, scenario):
    assert archive(client, scenario, year="active").status_code == 409
    assert archive(client, scenario).json()["detail"]["code"] == "ACADEMIC_YEAR_FINAL_EXPORT_REQUIRED"
    rows, _ = read_export(scenario.download(client, year="active"))
    assert rows["academic_year.csv"][0]["status"] == "ACTIVE"
    close_test_year(scenario)
    assert final_export(scenario, "active") is None
    result = archive(client, scenario, year="active")
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "ACADEMIC_YEAR_FINAL_EXPORT_REQUIRED"


def test_accepted_export_uses_the_closed_report_snapshot(client, scenario, monkeypatch):
    ready, resume = Event(), Event()
    write_archive = exports._write_archive

    def pause_export(db, output, school_id, year_id):
        ready.set()
        assert resume.wait(timeout=8)
        return write_archive(db, output, school_id, year_id)

    def download():
        with TestClient(app) as export_client:
            return scenario.download(export_client, year="active")

    monkeypatch.setattr(exports, "_write_archive", pause_export)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(download)
        try:
            assert ready.wait(timeout=8)
            closed_at = close_test_year(scenario)
            assert archive(client, scenario, year="active").status_code == 409
        finally:
            resume.set()
        rows, manifest = read_export(future.result(timeout=12))

    assert rows["academic_year.csv"][0]["status"] == "ACTIVE"
    assert datetime.fromisoformat(manifest["generated_at"]) >= closed_at
    assert final_export(scenario, "active") is None
    assert archive(client, scenario, year="active").status_code == 409

    expire_export_limit(scenario)
    rows, _ = read_export(scenario.download(client, year="active"))
    assert rows["academic_year.csv"][0]["status"] == "CLOSED"
    assert archive(client, scenario, year="active").status_code == 200


def test_failed_export_rolls_back_evidence(client, scenario):
    def fail_commit(db, _context):
        if any(isinstance(row, AcademicYearFinalExport) for row in db.new):
            db.execute(text("SELECT 1 / 0"))

    event.listen(Session, "after_flush", fail_commit)
    try:
        response = scenario.download(client)
    finally:
        event.remove(Session, "after_flush", fail_commit)
    assert response.status_code == 503
    assert final_export(scenario) is None
    rejected = archive(client, scenario)
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "ACADEMIC_YEAR_FINAL_EXPORT_REQUIRED"
    assert archive_audits(scenario) == []


def test_archive_failure_rolls_back_metadata_and_transition(client, scenario):
    assert scenario.download(client).status_code == 200
    export_id = final_export(scenario).export_id

    def fail_commit(db, _context):
        if any(isinstance(row, AuditLog) and row.action == ARCHIVE_ACTION for row in db.new):
            db.execute(text("SELECT 1 / 0"))

    key = uuid4()
    event.listen(Session, "after_flush", fail_commit)
    try:
        result = archive(client, scenario, key=key)
    finally:
        event.remove(Session, "after_flush", fail_commit)
    assert result.status_code == 503
    assert final_export(scenario).archive_idempotency_key is None
    assert final_export(scenario).export_id == export_id
    assert archive_audits(scenario) == []
    with SessionLocal() as db:
        assert db.get(AcademicYear, scenario.years["closed"]).status == AcademicYearStatus.CLOSED
    assert archive(client, scenario, key=key).status_code == 200
    assert len(archive_audits(scenario)) == 1


@pytest.mark.parametrize("actor,status", [(None, 401), ("student", 403), ("teacher", 403), ("owner", 403), ("unassigned", 403)])
def test_archive_requires_the_assigned_school_admin(client, scenario, actor, status):
    assert archive(client, scenario, actor=actor).status_code == status


def test_final_export_and_idempotency_are_school_scoped(client, scenario):
    close_test_year(scenario, "other")
    assert scenario.download(client, year="other", actor="other_admin").status_code == 200
    assert archive(client, scenario).status_code == 409
    assert archive(client, scenario, "other").status_code == 404
    assert archive(client, scenario, actor="other_admin").status_code == 404

    key = uuid4()
    assert archive(client, scenario, "other", actor="other_admin", key=key).status_code == 200
    assert scenario.download(client).status_code == 200
    assert archive(client, scenario, key=key).status_code == 200
    assert archive(client, scenario, year="active", key=key).status_code == 409


def test_archive_requires_boolean_acknowledgement_and_key(client, scenario):
    assert scenario.download(client).status_code == 200
    path = f"/academic-years/{scenario.years['closed']}/archive"
    assert client.post(path, headers=scenario.auth(), json={"export_saved": True}).status_code == 422
    assert archive(client, scenario, saved="true").status_code == 422
    assert archive(client, scenario, saved=False).status_code == 422
    assert final_export(scenario).archived_at is None


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_archives_commit_one_transition(client, scenario, monkeypatch, same_key):
    assert scenario.download(client).status_code == 200
    key = uuid4()
    other_key = key if same_key else uuid4()
    first, second = concurrent_requests(
        monkeypatch,
        lambda request_client: archive(request_client, scenario, key=key),
        lambda request_client: archive(request_client, scenario, actor="second_tab", key=other_key),
    )
    assert sorted([first.status_code, second.status_code]) == ([200, 200] if same_key else [200, 409])
    if same_key:
        assert first.json() == second.json()
    assert len(archive_audits(scenario)) == 1
    assert final_export(scenario).archived_at is not None


@pytest.mark.parametrize("year,target", [
    ("active", AcademicYearStatus.ARCHIVED),
    ("closed", AcademicYearStatus.ACTIVE),
    ("closed", AcademicYearStatus.ARCHIVED),
    ("archived", AcademicYearStatus.CLOSED),
    ("archived", AcademicYearStatus.ACTIVE),
])
def test_database_prohibits_reopening_and_unverified_archive(scenario, year, target):
    with pytest.raises(IntegrityError) as exc, SessionLocal.begin() as db:
        db.execute(update(AcademicYear).where(AcademicYear.id == scenario.years[year]).values(status=target))
    assert exc.value.orig.diag.constraint_name == "ck_academic_year_status_transition"
