import csv
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from queue import Queue
from threading import Barrier
from uuid import UUID
from zipfile import ZipFile

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.config.database import SessionLocal, engine
from app.finalize_academic_years import main
from app.models.academic_year import AcademicYear
from app.models.academic_year_closure_request import AcademicYearClosureRequest
from app.models.activity import Activity
from app.models.activity_attachment import ActivityAttachment
from app.models.activity_deadline_rejection import ActivityDeadlineRejection
from app.models.activity_review import ActivityReview
from app.models.activity_revision import ActivityRevision
from app.models.club import Club
from app.models.enums import (
    AcademicYearStatus,
    ActivityReviewDecision,
    ActivityRevisionStatus,
    ClubStatus,
    ClosureRequestStatus,
    ReviewMode,
)
from app.services.academic_year_closure_finalization import finalize_expired_closure_window
from test_academic_year_closure_workflow import add_activity, client, designated, scenario  # Reuse PostgreSQL fixtures.


def request_with_deadline(scenario, client, deadline, **kwargs):
    response = scenario.request(client, **kwargs)
    assert response.status_code == 201, response.text
    request_id = UUID(response.json()["id"])
    with SessionLocal.begin() as db:
        request = db.get(AcademicYearClosureRequest, request_id)
        request.requested_at = deadline - timedelta(hours=168)
        request.completion_deadline_at = deadline
    return request_id


def expired_window(scenario, client, **kwargs):
    with SessionLocal() as db:
        database_now = db.scalar(select(func.clock_timestamp()))
    return request_with_deadline(scenario, client, database_now - timedelta(hours=1), **kwargs)


def run_finalization(scenario, **kwargs):
    with SessionLocal() as db:
        return finalize_expired_closure_window(db, scenario.schools["own"], scenario.years["active"], **kwargs)


@pytest.mark.parametrize("offset_us,expected", [(-1, 0), (0, 1), (1, 1)])
def test_finalization_uses_the_exact_database_clock_boundary(scenario, client, designated, offset_us, expected):
    boundary = datetime(2026, 11, 30, 10, tzinfo=timezone.utc)
    request_id = request_with_deadline(scenario, client, boundary)
    with SessionLocal.begin() as db:
        _, _, (revision_id,) = add_activity(db, scenario, ActivityRevisionStatus.PENDING)

    def freeze_clock(_connection, _cursor, statement, parameters, _context, _executemany):
        if statement.startswith("SELECT clock_timestamp()"):
            return "SELECT CAST(%(clock_value)s AS timestamptz)", {
                "clock_value": boundary + timedelta(microseconds=offset_us),
            }
        return statement, parameters

    with SessionLocal() as db:
        connection = db.get_bind()
        event.listen(connection, "before_cursor_execute", freeze_clock, retval=True)
        try:
            count = finalize_expired_closure_window(db, scenario.schools["own"], scenario.years["active"])
        finally:
            event.remove(connection, "before_cursor_execute", freeze_clock)

    assert count == expected
    with SessionLocal() as db:
        request = db.get(AcademicYearClosureRequest, request_id)
        assert request.completion_deadline_at == boundary
        if expected:
            rejection = db.get(ActivityDeadlineRejection, revision_id)
            assert rejection.resolved_at == boundary + timedelta(microseconds=offset_us)
            assert request.finalized_at == rejection.resolved_at
        else:
            assert request.finalized_at is None
            assert db.get(ActivityDeadlineRejection, revision_id) is None
            assert db.get(ActivityRevision, revision_id).status == ActivityRevisionStatus.PENDING


def test_expiry_preserves_publication_attachments_and_manual_history(scenario, client, designated):
    request_id = expired_window(scenario, client)
    with SessionLocal.begin() as db:
        _, _, (approved, pending, draft, changes, rejected) = add_activity(
            db, scenario,
            ActivityRevisionStatus.APPROVED, ActivityRevisionStatus.PENDING, ActivityRevisionStatus.DRAFT,
            ActivityRevisionStatus.CHANGES_REQUESTED, ActivityRevisionStatus.REJECTED,
        )
        _, _, (inactive_pending,) = add_activity(
            db, scenario, ActivityRevisionStatus.PENDING, club_status=ClubStatus.ARCHIVED,
        )
        attachment = ActivityAttachment(
            revision_id=pending, file_name="agenda.pdf", file_type="application/pdf", storage_key="test/agenda",
        )
        historical_review = ActivityReview(
            revision_id=rejected, actor_account_id=scenario.accounts["teacher"], review_mode=ReviewMode.DIRECT,
            decision=ActivityReviewDecision.REJECTED, comments="Manual review reason.",
        )
        db.add_all([attachment, historical_review])
        db.flush()
        attachment_id = attachment.id
        manual_review_id = historical_review.id

    assert run_finalization(scenario) == 2
    assert run_finalization(scenario) == 0

    with SessionLocal() as db:
        request = db.get(AcademicYearClosureRequest, request_id)
        history = db.scalars(select(ActivityDeadlineRejection).where(
            ActivityDeadlineRejection.closure_request_id == request_id,
        )).all()
        assert {row.revision_id for row in history} == {pending, inactive_pending}
        for row in history:
            assert row.reason == "Academic-year deadline expired"
            assert row.resolved_at >= request.completion_deadline_at
            assert db.get(ActivityRevision, row.revision_id).status == ActivityRevisionStatus.REJECTED
        assert db.get(ActivityRevision, approved).status == ActivityRevisionStatus.APPROVED
        assert db.get(ActivityRevision, approved).is_published
        assert db.get(ActivityRevision, draft).status == ActivityRevisionStatus.DRAFT
        assert db.get(ActivityRevision, changes).status == ActivityRevisionStatus.CHANGES_REQUESTED
        assert db.get(ActivityAttachment, attachment_id).revision_id == pending
        assert db.get(ActivityReview, manual_review_id).comments == "Manual review reason."
        assert not db.scalars(select(ActivityReview).where(
            ActivityReview.revision_id.in_((pending, inactive_pending)),
        )).all()
        assert request.finalized_at is not None
        assert request.status == ClosureRequestStatus.PENDING
        assert db.get(AcademicYear, scenario.years["active"]).status == AcademicYearStatus.ACTIVE

    response = client.post(f"/academic-years/{scenario.years['active']}/export", headers=scenario.auth("admin"))
    assert response.status_code == 200, response.text
    with ZipFile(BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        rows = list(csv.DictReader(StringIO(archive.read("activity_deadline_rejections.csv").decode())))
        assert {row["revision_id"] for row in rows} == {str(pending), str(inactive_pending)}
        for row in rows:
            assert row["source"] == "SYSTEM"
            assert row["decision"] == "REJECTED"
            assert row["reason"] == "Academic-year deadline expired"
            assert row["closure_request_id"] == str(request_id)
        manual_rows = list(csv.DictReader(StringIO(archive.read("activity_reviews.csv").decode())))
        assert {row["revision_id"] for row in manual_rows} == {str(rejected)}
        manifest = json.loads(archive.read("manifest.json"))
        assert any(row["name"] == "activity_deadline_rejections.csv" and row["rows"] == 2 for row in manifest["files"])


def test_scheduler_processes_bounded_batches_until_the_window_is_complete(scenario, client, designated, capsys):
    request_id = expired_window(scenario, client)
    with SessionLocal.begin() as db:
        add_activity(db, scenario, ActivityRevisionStatus.PENDING, ActivityRevisionStatus.PENDING)

    assert main(["--limit", "1", "--batch-size", "1"]) == 0
    assert "1 automatic rejections" in capsys.readouterr().out
    with SessionLocal() as db:
        assert db.get(AcademicYearClosureRequest, request_id).finalized_at is None

    assert main(["--limit", "1", "--batch-size", "1"]) == 0
    assert "1 automatic rejections" in capsys.readouterr().out
    with SessionLocal() as db:
        assert db.get(AcademicYearClosureRequest, request_id).finalized_at is not None

    assert main(["--limit", "1", "--batch-size", "1"]) == 0
    assert "Examined 0 expired windows" in capsys.readouterr().out


def test_finalization_and_reports_are_scoped_to_the_requested_school(scenario, client, designated):
    request_id = expired_window(scenario, client)
    assert scenario.designate(client, teacher="other_teacher", actor="other_admin").status_code == 200
    other_request_id = expired_window(scenario, client, year="other", actor="other_teacher")
    with SessionLocal.begin() as db:
        _, _, (own_revision,) = add_activity(db, scenario, ActivityRevisionStatus.PENDING)
        other_club = Club(
            school_id=scenario.schools["other"], academic_year_id=scenario.years["other"],
            created_by=scenario.accounts["other_teacher"], name="Other school club", category="STEM",
        )
        db.add(other_club)
        db.flush()
        other_activity = Activity(club_id=other_club.id, created_by=scenario.accounts["other_teacher"])
        db.add(other_activity)
        db.flush()
        other_revision = ActivityRevision(
            activity_id=other_activity.id, revision_number=1, status=ActivityRevisionStatus.PENDING,
        )
        db.add(other_revision)
        db.flush()
        other_revision_id = other_revision.id

    with SessionLocal() as db:
        assert finalize_expired_closure_window(db, scenario.schools["own"], scenario.years["other"]) == 0
    assert run_finalization(scenario) == 1
    with SessionLocal() as db:
        assert db.get(ActivityRevision, other_revision_id).status == ActivityRevisionStatus.PENDING
        assert db.get(AcademicYearClosureRequest, other_request_id).finalized_at is None
    with SessionLocal() as db:
        assert finalize_expired_closure_window(db, scenario.schools["other"], scenario.years["other"]) == 1

    response = client.post(f"/academic-years/{scenario.years['active']}/export", headers=scenario.auth("admin"))
    assert response.status_code == 200, response.text
    with ZipFile(BytesIO(response.content)) as archive:
        rows = list(csv.DictReader(StringIO(archive.read("activity_deadline_rejections.csv").decode())))
        row, = rows
        assert row["revision_id"] == str(own_revision)
        assert row["closure_request_id"] == str(request_id)


def test_concurrent_finalizers_record_each_system_action_once(scenario, client, designated):
    request_id = expired_window(scenario, client)
    with SessionLocal.begin() as db:
        add_activity(db, scenario, ActivityRevisionStatus.PENDING, ActivityRevisionStatus.PENDING)

    ready, pids = Barrier(2, timeout=8), Queue()

    def finalize():
        def synchronize(_session, _transaction, connection):
            pids.put(connection.scalar(select(func.pg_backend_pid())))
            ready.wait()

        with SessionLocal() as db:
            event.listen(db, "after_begin", synchronize)
            return finalize_expired_closure_window(db, scenario.schools["own"], scenario.years["active"])

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(finalize) for _ in range(2)]
        assert sorted(future.result(timeout=15) for future in futures) == [0, 2]
    assert pids.get_nowait() != pids.get_nowait()

    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ActivityDeadlineRejection).where(
            ActivityDeadlineRejection.closure_request_id == request_id,
        )) == 2


def test_failed_finalization_rolls_back_outcomes_and_can_retry(scenario, client, designated):
    request_id = expired_window(scenario, client)
    with SessionLocal.begin() as db:
        _, _, (revision_id,) = add_activity(db, scenario, ActivityRevisionStatus.PENDING)

    def abort_after_update(_connection, cursor, statement, _parameters, _context, _executemany):
        if "INSERT INTO activity_deadline_rejections" in statement:
            cursor.execute("SELECT 1 / 0")

    event.listen(engine, "after_cursor_execute", abort_after_update)
    try:
        with pytest.raises(SQLAlchemyError):
            run_finalization(scenario)
    finally:
        event.remove(engine, "after_cursor_execute", abort_after_update)

    with SessionLocal() as db:
        assert db.get(AcademicYearClosureRequest, request_id).finalized_at is None
        assert db.get(ActivityDeadlineRejection, revision_id) is None
        assert db.get(ActivityRevision, revision_id).status == ActivityRevisionStatus.PENDING
    assert run_finalization(scenario) == 1
