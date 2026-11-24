from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from queue import Queue
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import DateTime, delete, literal, select

from app.config.database import SessionLocal, get_db
from app.models.academic_year import AcademicYear
from app.main import app
from app.models.academic_year_closure_request import AcademicYearClosureRequest
from app.models.account import Account
from app.models.activity import Activity
from app.models.activity_attachment import ActivityAttachment
from app.models.activity_review import ActivityReview
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.enums import (
    AcademicYearStatus,
    ActivityReviewDecision,
    ActivityRevisionStatus,
    ClosureRequestStatus,
    ClubReviewDecision,
    ClubRole,
    ClubStatus,
    MembershipStatus,
)
from app.routes.auth import require_account
from app.schemas.activity import ActivityDraftCreate, ActivityReviewRequest
from app.schemas.club import ClubRegistrationRequest, ClubReviewRequest
from app.services import academic_year_write_guard
from app.services.activity_attachments import upload_attachment
from app.services.activity_corrections import create_corrected_draft
from app.services.activity_drafts import ActivityError, create_activity_draft
from app.services.activity_replacements import create_replacement_draft
from app.services.activity_review import review_activity
from app.services.activity_workflow import edit_activity_draft, submit_activity_draft
from app.services.club_registration import ClubRequestError, register_club
from app.services.club_resubmission import resubmit_club
from app.services.club_review import review_club
from app.services.representative_memberships import end_representative, rejoin_representative
from app.services.storage.memory import MemoryAttachmentStorage
from app.services.teacher_invitation import respond_to_invitation
from app.services.teacher_replacement import replace_declined_teacher
from attachment_samples import sample_pdf
from test_academic_year_write_guard import lock_year, prepare_transaction, scenario, wait_for_blocking


REQUESTED_AT = datetime(2026, 11, 23, 10, tzinfo=timezone.utc)
DEADLINE = REQUESTED_AT + timedelta(hours=168)
JUST_BEFORE_DEADLINE = DEADLINE - timedelta(microseconds=1)
DEADLINE_ERROR = {
    "code": "ACADEMIC_YEAR_COMPLETION_WINDOW_ENDED",
    "message": "The academic-year completion window has ended.",
    "completion_deadline_at": DEADLINE.isoformat(),
}
ACTIVITY_DETAILS = ActivityDraftCreate(
    title="Updated workshop", activity_date=REQUESTED_AT.date(), category="STEM", description="Workshop details",
)
ACTIVITY_MUTATION_CASES = [
    ("create", ActivityRevisionStatus.DRAFT),
    ("edit", ActivityRevisionStatus.DRAFT),
    ("submit", ActivityRevisionStatus.DRAFT),
    ("correction", ActivityRevisionStatus.CHANGES_REQUESTED),
    ("replacement", ActivityRevisionStatus.APPROVED),
    ("upload", ActivityRevisionStatus.DRAFT),
    *((decision, ActivityRevisionStatus.PENDING) for decision in ActivityReviewDecision),
]
CLUB_MUTATION_CASES = [
    ("register", ClubStatus.ACTIVE, MembershipStatus.ACTIVE),
    ("review", ClubStatus.PENDING, MembershipStatus.ACTIVE),
    ("resubmit", ClubStatus.CHANGES_REQUESTED, MembershipStatus.ACTIVE),
    ("invitation", ClubStatus.ACTIVE, MembershipStatus.PENDING),
    ("replace_teacher", ClubStatus.ACTIVE, MembershipStatus.DECLINED),
    ("end", ClubStatus.ACTIVE, MembershipStatus.ACTIVE),
    ("rejoin", ClubStatus.ACTIVE, MembershipStatus.ENDED),
]


@pytest.fixture
def clock(monkeypatch):
    frozen = SimpleNamespace(now=JUST_BEFORE_DEADLINE)

    def timestamp():
        return literal(frozen.now, type_=DateTime(timezone=True))

    monkeypatch.setattr(academic_year_write_guard, "func", SimpleNamespace(clock_timestamp=timestamp))
    return frozen


@pytest.fixture
def window(scenario):
    with SessionLocal.begin() as db:
        request = AcademicYearClosureRequest(
            school_id=scenario.school_id,
            academic_year_id=scenario.year_id,
            teacher_account_id=scenario.teacher_id,
            designation_id=uuid4(),
            idempotency_key=uuid4(),
            requested_at=REQUESTED_AT,
            completion_deadline_at=DEADLINE,
            status=ClosureRequestStatus.PENDING,
        )
        db.add(request)
        db.flush()
        request_id = request.id

    try:
        yield request_id
    finally:
        with SessionLocal.begin() as db:
            revisions = (
                select(ActivityRevision.id)
                .join(Activity, Activity.id == ActivityRevision.activity_id)
                .join(Club, Club.id == Activity.club_id)
                .where(Club.school_id == scenario.school_id)
            )
            db.execute(delete(ActivityAttachment).where(ActivityAttachment.revision_id.in_(revisions)))
            db.execute(delete(AcademicYearClosureRequest).where(AcademicYearClosureRequest.id == request_id))


def activity_writer(db, scenario, operation, *, key, storage):
    target = scenario.first
    student = db.get(Account, scenario.student_id)
    args = (db, student, target.club_id, target.activity_id, target.revision_id)
    if isinstance(operation, ActivityReviewDecision):
        teacher = db.get(Account, scenario.teacher_id)
        payload = ActivityReviewRequest(decision=operation, comments="Reviewed the submission.")
        return review_activity(db, teacher, target.club_id, target.activity_id, target.revision_id, payload, key)

    writes = {
        "create": lambda: create_activity_draft(db, student, target.club_id, ACTIVITY_DETAILS, key),
        "edit": lambda: edit_activity_draft(*args, ACTIVITY_DETAILS),
        "submit": lambda: submit_activity_draft(*args),
        "correction": lambda: create_corrected_draft(*args, ACTIVITY_DETAILS, key),
        "replacement": lambda: create_replacement_draft(*args, ACTIVITY_DETAILS, key),
        "upload": lambda: upload_attachment(
            db, storage, student, target.club_id, target.activity_id, target.revision_id,
            key, "report.pdf", "application/pdf", BytesIO(sample_pdf()),
        ),
    }
    return writes[operation]()


@pytest.mark.parametrize("operation,initial_status", ACTIVITY_MUTATION_CASES)
@pytest.mark.parametrize("now", [JUST_BEFORE_DEADLINE, DEADLINE, DEADLINE + timedelta(microseconds=1)])
def test_activity_mutations_use_the_completion_window(scenario, window, clock, operation, initial_status, now):
    target = scenario.first
    with SessionLocal.begin() as db:
        revision = db.get(ActivityRevision, target.revision_id)
        revision.status = initial_status
        revision.is_published = initial_status == ActivityRevisionStatus.APPROVED

    clock.now = now
    storage = MemoryAttachmentStorage()
    with SessionLocal() as db:
        if now < DEADLINE:
            outcome = activity_writer(db, scenario, operation, key=uuid4(), storage=storage)
            assert outcome is not None
        else:
            with pytest.raises(ActivityError) as exc:
                activity_writer(db, scenario, operation, key=uuid4(), storage=storage)
            assert (exc.value.status_code, exc.value.detail) == (409, DEADLINE_ERROR)

    with SessionLocal() as db:
        revision = db.get(ActivityRevision, target.revision_id)
        if now >= DEADLINE:
            assert revision.status == initial_status
            assert revision.title == "Robotics workshop"
            assert db.scalar(select(ActivityAttachment.id).where(ActivityAttachment.revision_id == revision.id)) is None
        elif operation == "submit":
            assert revision.status == ActivityRevisionStatus.PENDING
            assert revision.submitted_at == now
        elif isinstance(operation, ActivityReviewDecision):
            review = db.get(ActivityReview, outcome["review_id"])
            assert review.reviewed_at == now
            assert revision.status.value == operation.value
        request = db.get(AcademicYearClosureRequest, window)
        assert request.requested_at == REQUESTED_AT
        assert request.completion_deadline_at == DEADLINE


@pytest.mark.parametrize("operation,initial_status", [
    ("create", ActivityRevisionStatus.DRAFT),
    ("edit", ActivityRevisionStatus.DRAFT),
    ("submit", ActivityRevisionStatus.DRAFT),
    ("correction", ActivityRevisionStatus.CHANGES_REQUESTED),
    ("replacement", ActivityRevisionStatus.APPROVED),
    ("upload", ActivityRevisionStatus.DRAFT),
    (ActivityReviewDecision.APPROVED, ActivityRevisionStatus.PENDING),
])
def test_existing_activity_writes_can_be_replayed_at_cutoff(scenario, window, clock, operation, initial_status):
    with SessionLocal.begin() as db:
        revision = db.get(ActivityRevision, scenario.first.revision_id)
        revision.status = initial_status
        revision.is_published = initial_status == ActivityRevisionStatus.APPROVED

    key = uuid4()
    storage = MemoryAttachmentStorage()
    with SessionLocal() as db:
        first = activity_writer(db, scenario, operation, key=key, storage=storage)

    clock.now = DEADLINE
    with SessionLocal() as db:
        assert activity_writer(db, scenario, operation, key=key, storage=storage) == first


def test_finalized_window_stays_frozen_if_database_clock_moves_backward(scenario, window, clock):
    with SessionLocal.begin() as db:
        db.get(AcademicYearClosureRequest, window).finalized_at = DEADLINE

    with SessionLocal() as db, pytest.raises(ActivityError) as error:
        activity_writer(db, scenario, "submit", key=uuid4(), storage=MemoryAttachmentStorage())
    assert (error.value.status_code, error.value.detail) == (409, DEADLINE_ERROR)


def prepare_club_mutation(scenario, club_status, teacher_membership_status):
    club_id = scenario.first.club_id
    with SessionLocal.begin() as db:
        db.get(Club, club_id).status = club_status
        teacher_membership = db.scalar(select(ClubMembership).where(
            ClubMembership.club_id == club_id, ClubMembership.role == ClubRole.TEACHER_REP,
        ))
        teacher_membership.status = teacher_membership_status
        return teacher_membership.id


def club_writer(db, scenario, operation, teacher_membership_id):
    club_id = scenario.first.club_id
    student = db.get(Account, scenario.student_id)
    teacher = db.get(Account, scenario.teacher_id)
    admin = db.get(Account, scenario.admin_id)
    payload = ClubRegistrationRequest(name="Robotics club", category="STEM", teacher_account_id=teacher.id)
    review = ClubReviewRequest(decision=ClubReviewDecision.REJECTED, comments="Please submit a new request.")
    writes = {
        "register": lambda: register_club(db, student, payload, uuid4()),
        "review": lambda: review_club(db, admin, club_id, review, uuid4()),
        "resubmit": lambda: resubmit_club(db, student, club_id, payload, uuid4()),
        "invitation": lambda: respond_to_invitation(db, teacher, club_id, "ACCEPT"),
        "replace_teacher": lambda: replace_declined_teacher(db, student, club_id, teacher.id, uuid4()),
        "end": lambda: end_representative(db, admin, club_id, teacher_membership_id, uuid4(), None),
        "rejoin": lambda: rejoin_representative(db, admin, club_id, teacher_membership_id, uuid4()),
    }
    return writes[operation]()


@pytest.mark.parametrize("operation,club_status,teacher_membership_status", CLUB_MUTATION_CASES)
def test_club_management_mutations_stop_at_cutoff(scenario, window, clock, operation, club_status, teacher_membership_status):
    teacher_membership_id = prepare_club_mutation(scenario, club_status, teacher_membership_status)
    clock.now = DEADLINE
    with SessionLocal() as db, pytest.raises(ClubRequestError) as exc:
        club_writer(db, scenario, operation, teacher_membership_id)
    assert (exc.value.status_code, exc.value.detail) == (409, DEADLINE_ERROR)


def put_target_in_history(scenario, state):
    with SessionLocal.begin() as db:
        year = db.get(AcademicYear, scenario.year_id)
        year.status = AcademicYearStatus.CLOSED
        if state == AcademicYearStatus.ARCHIVED:
            # Legacy archived fixtures have no export receipt or archived timestamp.
            archived = AcademicYear(
                school_id=scenario.school_id, name="Archived fixture", status=state,
                start_date=year.start_date, end_date=year.end_date,
            )
            db.add(archived)
            db.flush()
            db.get(Club, scenario.first.club_id).academic_year_id = archived.id


@pytest.mark.parametrize("state", [AcademicYearStatus.CLOSED, AcademicYearStatus.ARCHIVED])
@pytest.mark.parametrize("operation,initial_status", ACTIVITY_MUTATION_CASES)
def test_historical_activity_mutations_preserve_records(scenario, state, operation, initial_status):
    target = scenario.first
    with SessionLocal.begin() as db:
        revision = db.get(ActivityRevision, target.revision_id)
        revision.status = initial_status
        revision.is_published = initial_status == ActivityRevisionStatus.APPROVED
    put_target_in_history(scenario, state)

    with SessionLocal() as db, pytest.raises(ActivityError) as exc:
        activity_writer(db, scenario, operation, key=uuid4(), storage=MemoryAttachmentStorage())
    assert exc.value.status_code == 409

    with SessionLocal() as db:
        revision, = db.scalars(select(ActivityRevision).where(ActivityRevision.activity_id == target.activity_id)).all()
        assert (revision.id, revision.status, revision.title) == (target.revision_id, initial_status, "Robotics workshop")
        assert db.scalar(select(ActivityAttachment.id).where(ActivityAttachment.revision_id == revision.id)) is None
        assert db.scalar(select(ActivityReview.id).where(ActivityReview.revision_id == revision.id)) is None
        assert db.scalar(select(AuditLog.id).where(AuditLog.actor_account_id.in_((scenario.student_id, scenario.teacher_id)))) is None


@pytest.mark.parametrize("state", [AcademicYearStatus.CLOSED, AcademicYearStatus.ARCHIVED])
@pytest.mark.parametrize("operation,club_status,teacher_membership_status", CLUB_MUTATION_CASES)
def test_historical_club_mutations_preserve_records(scenario, state, operation, club_status, teacher_membership_status):
    membership_id = prepare_club_mutation(scenario, club_status, teacher_membership_status)
    put_target_in_history(scenario, state)

    with SessionLocal() as db, pytest.raises(ClubRequestError) as exc:
        club_writer(db, scenario, operation, membership_id)
    assert exc.value.status_code == 409

    with SessionLocal() as db:
        assert db.get(Club, scenario.first.club_id).status == club_status
        assert db.get(ClubMembership, membership_id).status == teacher_membership_status
        assert db.scalars(select(AuditLog.id).where(
            AuditLog.actor_account_id.in_((scenario.student_id, scenario.teacher_id, scenario.admin_id)),
        )).all() == []


@pytest.mark.parametrize("holding", ["year", "club", "revision"])
def test_submission_checks_time_after_waiting_for_target_locks(scenario, window, clock, holding):
    target = scenario.first
    pids = Queue()

    def submit():
        with SessionLocal() as db:
            pids.put(prepare_transaction(db))
            student = db.get(Account, scenario.student_id)
            return submit_activity_draft(db, student, target.club_id, target.activity_id, target.revision_id)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with SessionLocal.begin() as holder:
            holder_pid = prepare_transaction(holder)
            if holding == "year":
                lock_year(holder, scenario)
            elif holding == "club":
                academic_year_write_guard.lock_club_for_year_write(holder, scenario.school_id, target.club_id)
            else:
                holder.scalar(select(ActivityRevision).where(ActivityRevision.id == target.revision_id).with_for_update())
            outcome = executor.submit(submit)
            wait_for_blocking(pids.get(timeout=8), holder_pid)
            clock.now = DEADLINE

        with pytest.raises(ActivityError) as error:
            outcome.result(timeout=8)
    assert (error.value.status_code, error.value.detail) == (409, DEADLINE_ERROR)


def test_waiting_review_cannot_replace_publication_at_cutoff(scenario, window, clock):
    target = scenario.first
    payload = ActivityReviewRequest(decision=ActivityReviewDecision.APPROVED)
    with SessionLocal.begin() as db:
        published = db.get(ActivityRevision, target.revision_id)
        published.status = ActivityRevisionStatus.APPROVED
        published.is_published = True
        replacement = ActivityRevision(
            activity_id=target.activity_id, revision_number=2, title="Replacement workshop",
            activity_date=REQUESTED_AT.date(), category="STEM", description="Replacement details",
            status=ActivityRevisionStatus.PENDING,
        )
        db.add(replacement)
        db.flush()
        replacement_id = replacement.id

    pids = Queue()

    def review():
        with SessionLocal() as db:
            pids.put(prepare_transaction(db))
            teacher = db.get(Account, scenario.teacher_id)
            return review_activity(db, teacher, target.club_id, target.activity_id, replacement_id, payload, uuid4())

    with ThreadPoolExecutor(max_workers=1) as executor:
        with SessionLocal.begin() as holder:
            holder_pid = prepare_transaction(holder)
            holder.scalar(select(ActivityRevision).where(ActivityRevision.id == target.revision_id).with_for_update())
            reviewing = executor.submit(review)
            wait_for_blocking(pids.get(timeout=8), holder_pid)
            clock.now = DEADLINE

        with pytest.raises(ActivityError) as error:
            reviewing.result(timeout=8)
    assert (error.value.status_code, error.value.detail) == (409, DEADLINE_ERROR)

    with SessionLocal() as db:
        published = db.get(ActivityRevision, target.revision_id)
        replacement = db.get(ActivityRevision, replacement_id)
        assert published.status == ActivityRevisionStatus.APPROVED
        assert published.is_published
        assert replacement.status == ActivityRevisionStatus.PENDING
        assert not replacement.is_published
        assert db.scalar(select(ActivityReview.id).where(ActivityReview.revision_id == replacement_id)) is None


@pytest.mark.parametrize("closed", [False, True])
def test_submission_http_error_contains_a_stable_code_and_scopes_deadlines(scenario, window, clock, monkeypatch, closed):
    clock.now = DEADLINE
    if closed:
        with SessionLocal.begin() as db:
            year = db.get(AcademicYear, scenario.year_id)
            year.status = AcademicYearStatus.CLOSED
            request = db.get(AcademicYearClosureRequest, window)
            request.status = ClosureRequestStatus.CONFIRMED
            request.finalized_at = DEADLINE
            request.confirmed_at = DEADLINE
            request.confirmed_by_account_id = scenario.admin_id

    target = scenario.first
    path = f"/clubs/{target.club_id}/activities/{target.activity_id}/revisions/{target.revision_id}/submit"
    account_id = scenario.student_id

    def viewer(db=Depends(get_db)):
        return db.get(Account, account_id)

    monkeypatch.setitem(app.dependency_overrides, require_account, viewer)
    with TestClient(app) as client:
        expired = client.post(path)
        assert expired.status_code == 409
        assert expired.json() == {"detail": DEADLINE_ERROR}

        account_id = scenario.outsider_id
        forbidden = client.post(path)
        assert forbidden.status_code == 404
        assert forbidden.json() == {"detail": "Club not found."}
