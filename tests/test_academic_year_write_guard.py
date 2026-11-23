from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from queue import Queue
from threading import Event
from time import monotonic, sleep
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import OperationalError

from app.config.database import SessionLocal, engine
from app.models.academic_year import AcademicYear
from app.models.account import Account
from app.models.activity import Activity
from app.models.activity_review import ActivityReview
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
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
    MembershipStatus,
)
from app.models.school import School
from app.models.school_admin import SchoolAdmin
from app.schemas.activity import ActivityReviewRequest
from app.schemas.club import ClubRegistrationRequest, ClubReviewRequest
from app.services.activity_drafts import ActivityError
from app.services.activity_review import review_activity
from app.services.activity_workflow import submit_activity_draft
from app.services.club_registration import ClubRequestError, register_club
from app.services.club_review import review_club
from app.services.representative_memberships import end_representative
from app.services.teacher_invitation import respond_to_invitation


WAIT_SECONDS = 8


@dataclass(frozen=True)
class ActivityTarget:
    club_id: UUID
    activity_id: UUID
    revision_id: UUID


@dataclass(frozen=True)
class Scenario:
    school_id: UUID
    year_id: UUID
    historical_year_id: UUID
    student_id: UUID
    teacher_id: UUID
    admin_id: UUID
    outsider_id: UUID
    first: ActivityTarget
    second: ActivityTarget
    other_school: ActivityTarget


@pytest.fixture
def scenario():
    if engine.url.get_backend_name() != "postgresql" or engine.url.database != "clubos_test":
        pytest.fail("Use the clubos_test PostgreSQL database")

    suffix = uuid4().hex[:12]
    today = date.today()
    with SessionLocal.begin() as db:
        school = School(name="Year Lock School", school_code=f"CLB-{suffix}")
        other_school = School(name="Other Lock School", school_code=f"CLB-{suffix}X")
        db.add_all([school, other_school])
        db.flush()
        school_ids = (school.id, other_school.id)
        student, teacher, admin, outsider = actors = [
            Account(
                school_id=school_id,
                account_type=role,
                full_name=name,
                username=f"{name}_{suffix}",
                email=f"{name}_{suffix}@example.com",
                password_hash="test-only",
            )
            for name, role, school_id in (
                ("student", AccountType.STUDENT, school.id),
                ("teacher", AccountType.TEACHER, school.id),
                ("admin", AccountType.SCHOOL_ADMIN, school.id),
                ("outsider", AccountType.STUDENT, other_school.id),
            )
        ]
        year, historical_year, other_year = years = [
            AcademicYear(
                school_id=school_id,
                name=status.value,
                start_date=today - timedelta(days=30),
                end_date=today + timedelta(days=30),
                status=status,
            )
            for school_id, status in (
                (school.id, AcademicYearStatus.ACTIVE),
                (school.id, AcademicYearStatus.CLOSED),
                (other_school.id, AcademicYearStatus.ACTIVE),
            )
        ]
        db.add_all([*actors, *years])
        db.flush()
        db.add(SchoolAdmin(school_id=school.id, account_id=admin.id))

        targets = []
        for name, club_year, creator in (
            ("First Club", year, student),
            ("Second Club", year, student),
            ("Other Club", other_year, outsider),
        ):
            club = Club(
                school_id=club_year.school_id, academic_year_id=club_year.id, created_by=creator.id,
                name=name, category="STEM", status=ClubStatus.ACTIVE,
            )
            db.add(club)
            db.flush()
            db.add(ClubMembership(
                club_id=club.id, account_id=creator.id, role=ClubRole.STUDENT_REP,
                status=MembershipStatus.ACTIVE, start_date=today,
            ))
            if club_year.id == year.id:
                db.add(ClubMembership(
                    club_id=club.id, account_id=teacher.id, role=ClubRole.TEACHER_REP,
                    status=MembershipStatus.ACTIVE, start_date=today,
                ))
            activity = Activity(club_id=club.id, created_by=creator.id)
            db.add(activity)
            db.flush()
            revision = ActivityRevision(
                activity_id=activity.id, revision_number=1, title="Robotics workshop", activity_date=today,
                description="Test workshop", category="STEM", status=ActivityRevisionStatus.DRAFT,
            )
            db.add(revision)
            db.flush()
            targets.append(ActivityTarget(club.id, activity.id, revision.id))

        result = Scenario(school.id, year.id, historical_year.id, student.id, teacher.id, admin.id, outsider.id, *targets)

    try:
        yield result
    finally:
        with SessionLocal.begin() as db:
            account_ids = select(Account.id).where(Account.school_id.in_(school_ids))
            club_ids = select(Club.id).where(Club.school_id.in_(school_ids))
            activity_ids = select(Activity.id).where(Activity.club_id.in_(club_ids))
            revision_ids = select(ActivityRevision.id).where(ActivityRevision.activity_id.in_(activity_ids))
            db.execute(text("SET LOCAL statement_timeout = '10s'"))
            db.execute(delete(AuditLog).where(AuditLog.actor_account_id.in_(account_ids)))
            db.execute(delete(ActivityReview).where(ActivityReview.revision_id.in_(revision_ids)))
            db.execute(delete(ActivityRevision).where(ActivityRevision.activity_id.in_(activity_ids)))
            db.execute(delete(Activity).where(Activity.club_id.in_(club_ids)))
            db.execute(delete(ClubReview).where(ClubReview.club_id.in_(club_ids)))
            db.execute(delete(ClubMembership).where(ClubMembership.club_id.in_(club_ids)))
            db.execute(delete(Club).where(Club.school_id.in_(school_ids)))
            db.execute(delete(AcademicYear).where(AcademicYear.school_id.in_(school_ids)))
            db.execute(delete(SchoolAdmin).where(SchoolAdmin.school_id.in_(school_ids)))
            db.execute(delete(Account).where(Account.school_id.in_(school_ids)))
            db.execute(delete(School).where(School.id.in_(school_ids)))


def prepare_transaction(db):
    db.execute(text("SET LOCAL statement_timeout = '12s'"))
    return db.scalar(select(func.pg_backend_pid()))


def run_writer(
    scenario, target, pids, *, operation="submit", release=None, actor_id=None, cache_year=False, review_key=None,
):
    with SessionLocal() as db:
        pid = prepare_transaction(db)
        default_actor = scenario.student_id if operation == "submit" else scenario.teacher_id
        actor = db.get(Account, actor_id or default_actor)
        if cache_year:
            # Keep the pre-transition ORM row in this session while the new lock waits.
            cached_year = db.get(AcademicYear, scenario.year_id)
            assert cached_year.status == AcademicYearStatus.ACTIVE

        if release is None:
            pids.put(pid)
        else:
            def wait_before_commit(_session):
                pids.put(pid)
                assert release.wait(timeout=WAIT_SECONDS)

            event.listen(db, "before_commit", wait_before_commit)

        args = (db, actor, target.club_id, target.activity_id, target.revision_id)
        if operation == "submit":
            return submit_activity_draft(*args)
        return review_activity(
            *args, ActivityReviewRequest(decision=ActivityReviewDecision.APPROVED), review_key or uuid4(),
        )


def lock_year(db, scenario, *, nowait=False):
    return db.scalars(
        select(AcademicYear)
        .where(AcademicYear.id == scenario.year_id, AcademicYear.school_id == scenario.school_id)
        .with_for_update(nowait=nowait)
    ).one()


def wait_for_blocking(waiting_pid, holder_pid):
    assert waiting_pid != holder_pid, "Workers must use independent PostgreSQL connections"
    deadline = monotonic() + WAIT_SECONDS
    with SessionLocal() as db:
        while monotonic() < deadline:
            blockers = db.scalar(select(func.pg_blocking_pids(waiting_pid)))
            if holder_pid in blockers:
                return
            sleep(0.01)
    pytest.fail(f"PostgreSQL backend {waiting_pid} did not wait for backend {holder_pid}")


def pending_after_exclusive_lock(scenario, pids, *, pause_after_check=None):
    # Test-only model of the future confirmation transaction and its fresh blocker read.
    with SessionLocal.begin() as db:
        pids.put(prepare_transaction(db))
        year = lock_year(db, scenario)
        pending_count = db.scalar(
            select(func.count(ActivityRevision.id))
            .join(Activity, Activity.id == ActivityRevision.activity_id)
            .join(Club, Club.id == Activity.club_id)
            .where(
                Club.school_id == scenario.school_id,
                Club.academic_year_id == scenario.year_id,
                ActivityRevision.status == ActivityRevisionStatus.PENDING,
            )
        )
        if pending_count == 0:
            year.status = AcademicYearStatus.CLOSED
        if pause_after_check is not None:
            checked, release = pause_after_check
            checked.set()
            assert release.wait(timeout=WAIT_SECONDS)
        return pending_count


def assert_no_open_transactions(*pids):
    with SessionLocal() as db:
        assert db.execute(text(
            "SELECT pid FROM pg_stat_activity "
            "WHERE pid = ANY(:pids) AND pid <> pg_backend_pid() AND xact_start IS NOT NULL"
        ), {"pids": list(pids)}).all() == []


def assert_revision_state(target, *, status, actions=(), reviews=0):
    with SessionLocal() as db:
        revision = db.scalars(select(ActivityRevision).where(ActivityRevision.activity_id == target.activity_id)).one()
        assert revision.id == target.revision_id
        assert revision.status == status
        assert db.scalars(select(AuditLog.action).where(AuditLog.entity_id == str(target.revision_id))).all() == list(actions)
        assert db.scalar(select(func.count(ActivityReview.id)).where(ActivityReview.revision_id == target.revision_id)) == reviews


def mark_revision_pending(target):
    with SessionLocal.begin() as db:
        revision = db.get(ActivityRevision, target.revision_id)
        revision.status = ActivityRevisionStatus.PENDING
        revision.submitted_at = datetime.now(timezone.utc)


def test_submissions_in_different_clubs_can_hold_the_same_year_lock(scenario):
    pids = Queue()
    release = Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        submissions = [
            executor.submit(run_writer, scenario, target, pids, release=release)
            for target in (scenario.first, scenario.second)
        ]
        try:
            first_pid, second_pid = (pids.get(timeout=WAIT_SECONDS) for _ in submissions)
            assert first_pid != second_pid
            with SessionLocal() as observer, pytest.raises(OperationalError) as exc:
                lock_year(observer, scenario, nowait=True)
            assert exc.value.orig.sqlstate == "55P03"
        finally:
            release.set()

        for submission in submissions:
            assert submission.result(timeout=WAIT_SECONDS)["status"] == ActivityRevisionStatus.PENDING

    replay = run_writer(scenario, scenario.first, Queue())
    assert replay["status"] == ActivityRevisionStatus.PENDING
    for target in (scenario.first, scenario.second):
        assert_revision_state(target, status=ActivityRevisionStatus.PENDING, actions=("ACTIVITY_SUBMITTED",))
    assert_no_open_transactions(first_pid, second_pid)


@pytest.mark.parametrize("operation", ["submit", "review"])
def test_exclusive_year_lock_waits_for_writers_and_reads_committed_blockers(scenario, operation):
    if operation == "review":
        mark_revision_pending(scenario.first)

    writer_pids, closer_pids = Queue(), Queue()
    release = Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(run_writer, scenario, scenario.first, writer_pids, operation=operation, release=release)
        try:
            writer_pid = writer_pids.get(timeout=WAIT_SECONDS)
            closer = executor.submit(pending_after_exclusive_lock, scenario, closer_pids)
            closer_pid = closer_pids.get(timeout=WAIT_SECONDS)
            wait_for_blocking(closer_pid, writer_pid)
        finally:
            release.set()

        result = writer.result(timeout=WAIT_SECONDS)
        pending_count = closer.result(timeout=WAIT_SECONDS)

    if operation == "submit":
        expected_status = ActivityRevisionStatus.PENDING
        expected_year = AcademicYearStatus.ACTIVE
        assert pending_count == 1
        assert_revision_state(scenario.first, status=expected_status, actions=("ACTIVITY_SUBMITTED",))
    else:
        expected_status = ActivityRevisionStatus.APPROVED
        expected_year = AcademicYearStatus.CLOSED
        assert pending_count == 0
        assert_revision_state(scenario.first, status=expected_status, actions=("ACTIVITY_REVIEWED",), reviews=1)

    assert result["status"] == expected_status
    with SessionLocal() as db:
        assert db.get(AcademicYear, scenario.year_id).status == expected_year
    assert_no_open_transactions(writer_pid, closer_pid)


def test_last_review_allows_closure_before_a_later_submission(scenario):
    mark_revision_pending(scenario.first)
    review_key = uuid4()
    review_pids, closure_pids, submission_pids = Queue(), Queue(), Queue()
    commit_review, closure_checked, commit_closure = Event(), Event(), Event()
    with ThreadPoolExecutor(max_workers=3) as executor:
        try:
            review = executor.submit(
                run_writer, scenario, scenario.first, review_pids,
                operation="review", release=commit_review, review_key=review_key,
            )
            review_pid = review_pids.get(timeout=WAIT_SECONDS)
            closer = executor.submit(
                pending_after_exclusive_lock, scenario, closure_pids,
                pause_after_check=(closure_checked, commit_closure),
            )
            closer_pid = closure_pids.get(timeout=WAIT_SECONDS)
            wait_for_blocking(closer_pid, review_pid)

            commit_review.set()
            assert closure_checked.wait(timeout=WAIT_SECONDS)
            submission = executor.submit(run_writer, scenario, scenario.second, submission_pids)
            submission_pid = submission_pids.get(timeout=WAIT_SECONDS)
            wait_for_blocking(submission_pid, closer_pid)
        finally:
            commit_review.set()
            commit_closure.set()

        assert review.result(timeout=WAIT_SECONDS)["status"] == ActivityRevisionStatus.APPROVED
        assert closer.result(timeout=WAIT_SECONDS) == 0
        with pytest.raises(ActivityError) as exc:
            submission.result(timeout=WAIT_SECONDS)
        assert (exc.value.status_code, exc.value.detail) == (409, "Academic year is not active.")

    with pytest.raises(ActivityError) as exc:
        run_writer(scenario, scenario.first, Queue(), operation="review", review_key=review_key)
    assert (exc.value.status_code, exc.value.detail) == (409, "Academic year is not active.")
    with SessionLocal() as db:
        assert db.get(AcademicYear, scenario.year_id).status == AcademicYearStatus.CLOSED
    assert_revision_state(
        scenario.first, status=ActivityRevisionStatus.APPROVED, actions=("ACTIVITY_REVIEWED",), reviews=1,
    )
    assert_revision_state(scenario.second, status=ActivityRevisionStatus.DRAFT)
    assert_no_open_transactions(review_pid, closer_pid, submission_pid)


@pytest.mark.parametrize("operation", ["submit", "review"])
def test_writer_waits_before_locking_children_and_rechecks_closed_year(scenario, operation):
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with SessionLocal.begin() as closing:
            closer_pid = prepare_transaction(closing)
            year = lock_year(closing, scenario)
            writer = executor.submit(run_writer, scenario, scenario.first, pids, operation=operation, cache_year=True)
            writer_pid = pids.get(timeout=WAIT_SECONDS)
            wait_for_blocking(writer_pid, closer_pid)
            with SessionLocal() as probe:
                probe.scalars(select(Club.id).where(Club.id == scenario.first.club_id).with_for_update(nowait=True)).one()
                probe.scalars(select(ActivityRevision.id).where(
                    ActivityRevision.id == scenario.first.revision_id
                ).with_for_update(nowait=True)).one()
            year.status = AcademicYearStatus.CLOSED

        with pytest.raises(ActivityError) as exc:
            writer.result(timeout=WAIT_SECONDS)

    assert (exc.value.status_code, exc.value.detail) == (409, "Academic year is not active.")
    assert_revision_state(scenario.first, status=ActivityRevisionStatus.DRAFT)
    assert_no_open_transactions(writer_pid, closer_pid)


def test_writer_revalidates_the_clubs_year_after_acquiring_the_lock(scenario):
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with SessionLocal.begin() as moving:
            moving_pid = prepare_transaction(moving)
            lock_year(moving, scenario)
            writer = executor.submit(run_writer, scenario, scenario.first, pids)
            writer_pid = pids.get(timeout=WAIT_SECONDS)
            wait_for_blocking(writer_pid, moving_pid)
            moving.get(Club, scenario.first.club_id).academic_year_id = scenario.historical_year_id

        with pytest.raises(ActivityError) as exc:
            writer.result(timeout=WAIT_SECONDS)

    assert exc.value.status_code == 404
    assert_revision_state(scenario.first, status=ActivityRevisionStatus.DRAFT)
    assert_no_open_transactions(writer_pid, moving_pid)


def test_exclusive_year_lock_stays_within_the_school(scenario):
    pids = Queue()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with SessionLocal.begin() as owner:
            owner_pid = prepare_transaction(owner)
            lock_year(owner, scenario)
            writer = executor.submit(run_writer, scenario, scenario.first, pids, actor_id=scenario.outsider_id)
            writer_pid = pids.get(timeout=WAIT_SECONDS)
            assert writer_pid != owner_pid
            with pytest.raises(ActivityError) as exc:
                writer.result(timeout=WAIT_SECONDS)

            own_school_write = executor.submit(
                run_writer, scenario, scenario.other_school, pids, actor_id=scenario.outsider_id,
            )
            other_pid = pids.get(timeout=WAIT_SECONDS)
            assert other_pid != owner_pid
            assert own_school_write.result(timeout=WAIT_SECONDS)["status"] == ActivityRevisionStatus.PENDING

    assert (exc.value.status_code, exc.value.detail) == (404, "Club not found.")
    assert_revision_state(scenario.first, status=ActivityRevisionStatus.DRAFT)
    assert_revision_state(
        scenario.other_school, status=ActivityRevisionStatus.PENDING, actions=("ACTIVITY_SUBMITTED",),
    )
    assert_no_open_transactions(writer_pid, other_pid, owner_pid)


def test_club_registration_rechecks_active_year_and_preserves_replay(scenario):
    data = ClubRegistrationRequest(
        name="New Club", category="STEM", teacher_account_id=scenario.teacher_id,
    )
    key = uuid4()
    pids = Queue()

    def register(registration_key):
        with SessionLocal() as db:
            pids.put(prepare_transaction(db))
            return register_club(db, db.get(Account, scenario.student_id), data, registration_key)

    original = register(key)
    pids.get_nowait()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with SessionLocal.begin() as closing:
            closer_pid = prepare_transaction(closing)
            year = lock_year(closing, scenario)
            writer = executor.submit(register, uuid4())
            writer_pid = pids.get(timeout=WAIT_SECONDS)
            wait_for_blocking(writer_pid, closer_pid)
            with SessionLocal() as probe, pytest.raises(OperationalError) as exc:
                probe.scalar(select(School.id).where(School.id == scenario.school_id).with_for_update(nowait=True))
            assert exc.value.orig.sqlstate == "55P03"
            year.status = AcademicYearStatus.CLOSED

        with pytest.raises(ClubRequestError) as exc:
            writer.result(timeout=WAIT_SECONDS)

    assert (exc.value.status_code, exc.value.detail) == (409, "An active academic year is required.")
    assert register(key) == original
    with SessionLocal() as db:
        assert db.scalars(select(Club.id).where(Club.school_id == scenario.school_id, Club.name == data.name)).all() == [
            original["id"]
        ]
        audits = db.scalars(select(AuditLog.entity_id).where(
            AuditLog.actor_account_id == scenario.student_id,
            AuditLog.action == "CLUB_REGISTRATION_REQUESTED",
        )).all()
        assert audits == [str(original["id"])]
    assert_no_open_transactions(writer_pid, closer_pid)


@pytest.mark.parametrize("operation", ["reject_club", "accept_invitation", "end_representative"])
def test_club_writes_wait_for_year_and_reject_after_closure(scenario, operation):
    club_id = scenario.first.club_id
    club_status = ClubStatus.PENDING if operation == "reject_club" else ClubStatus.ACTIVE
    membership_status = MembershipStatus.PENDING if operation == "accept_invitation" else MembershipStatus.ACTIVE
    with SessionLocal.begin() as db:
        db.get(Club, club_id).status = club_status
        membership = db.scalars(select(ClubMembership).where(
            ClubMembership.club_id == club_id,
            ClubMembership.account_id == scenario.teacher_id,
        )).one()
        membership.status = membership_status
        membership_id = membership.id

    pids = Queue()

    def write():
        with SessionLocal() as db:
            pids.put(prepare_transaction(db))
            if operation == "accept_invitation":
                return respond_to_invitation(db, db.get(Account, scenario.teacher_id), club_id, "ACCEPT")
            admin = db.get(Account, scenario.admin_id)
            if operation == "reject_club":
                return review_club(
                    db, admin, club_id,
                    ClubReviewRequest(decision=ClubReviewDecision.REJECTED, comments="Needs more detail."), uuid4(),
                )
            return end_representative(db, admin, club_id, membership_id, uuid4(), reason=None)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with SessionLocal.begin() as closing:
            closer_pid = prepare_transaction(closing)
            year = lock_year(closing, scenario)
            writer = executor.submit(write)
            writer_pid = pids.get(timeout=WAIT_SECONDS)
            wait_for_blocking(writer_pid, closer_pid)
            with SessionLocal() as probe:
                probe.scalars(select(Club.id).where(Club.id == club_id).with_for_update(nowait=True)).one()
            year.status = AcademicYearStatus.CLOSED

        with pytest.raises(ClubRequestError) as exc:
            writer.result(timeout=WAIT_SECONDS)
    assert (exc.value.status_code, exc.value.detail) == (409, "Academic year is not active.")

    with pytest.raises(ClubRequestError) as exc:
        write()
    assert (exc.value.status_code, exc.value.detail) == (409, "Academic year is not active.")
    with SessionLocal() as db:
        assert db.get(Club, club_id).status == club_status
        membership = db.get(ClubMembership, membership_id)
        assert membership.status == membership_status
        assert membership.end_date is None
        assert not db.scalars(select(ClubReview.id).where(ClubReview.club_id == club_id)).all()
        assert not db.scalars(select(AuditLog.id).where(
            AuditLog.actor_account_id.in_((scenario.admin_id, scenario.teacher_id)),
        )).all()
    assert_no_open_transactions(writer_pid, closer_pid, pids.get_nowait())
