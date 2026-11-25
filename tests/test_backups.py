from datetime import date, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.config.database as database
from app.models import (
    AcademicYear, Account, Activity, ActivityAttachment, ActivityReview, ActivityRevision, AuditLog, AuthSession,
    Club, ClubMembership, ImpersonationSession, MaintenanceState, School, SchoolAdmin,
)
from app.models.enums import (
    AcademicYearStatus, AccountType, ActivityReviewDecision, ActivityRevisionStatus, ClubRole, ClubStatus,
    ImpersonationStatus, MembershipStatus, ReviewMode,
)
from app.services.activity_attachments import attachment_object_key
from app.services.activity_drafts import ActivityError
from app.services import backups
from app.services.backups import BackupError, create_backup, prune_backups, restore_backup, verify_backup
from app.services.history_retention import cleanup_history
from app.services.maintenance import MaintenanceError, bind_storage, exclusive_maintenance, validate_storage_pairing
from app.services.operation_receipts import find_receipt
from app.services.sessions import database_now, token_hash
from app.services.storage import StorageError
from app.services.storage.local import LocalAttachmentStorage
from attachment_samples import sample_pdf
from test_academic_year_history import Publication, add_attachment


@pytest.fixture
def disposable_databases():
    if database.engine.url.database != "clubos_test":
        pytest.fail("Backup tests require the isolated clubos_test cluster.")
    url = database.database_url
    options = {"host": url.host, "port": url.port, "user": url.username,
               "password": url.password, "dbname": "postgres", "autocommit": True}
    source, target = (f"clubos_restore_{kind}_{uuid4().hex[:12]}" for kind in ("source", "target"))
    with psycopg.connect(**options) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(source)))
    try:
        yield source, target
    finally:
        with psycopg.connect(**options) as connection:
            for name in (target, source):
                connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _seed_history(db: Session, storage: LocalAttachmentStorage) -> dict:
    now = database_now(db)
    school, other = (School(name=f"School {i}", school_code=f"BACKUP-{i}") for i in range(2))
    db.add_all([school, other])
    db.flush()
    admin, teacher, student = [Account(
        school_id=school.id, account_type=role, full_name=name, username=name,
        email=f"{name}@example.test", password_hash="fixture-only", is_active=True,
    ) for name, role in (("admin", AccountType.SCHOOL_ADMIN), ("teacher", AccountType.TEACHER), ("student", AccountType.STUDENT))]
    historical = AcademicYear(school_id=school.id, name="Historical year", start_date=date(2025, 6, 1),
                              end_date=date(2026, 5, 31), status=AcademicYearStatus.CLOSED)
    db.add_all([admin, teacher, student, historical])
    db.flush()
    club = Club(school_id=school.id, academic_year_id=historical.id, created_by=student.id,
                name="History Club", category="STEM", status=ClubStatus.ACTIVE)
    parent = AuthSession(account_id=admin.id, token_hash=token_hash("fixture-cookie", "fixture-tab"), expires_at=now + timedelta(hours=2))
    old = AuthSession(account_id=admin.id, token_hash=token_hash("fixture-old-cookie", "fixture-old-tab"),
                      expires_at=now - timedelta(days=2), revoked_at=now - timedelta(days=3))
    db.add_all([SchoolAdmin(school_id=school.id, account_id=admin.id), club, parent, old])
    db.flush()
    impersonation = ImpersonationSession(school_id=school.id, admin_account_id=admin.id, target_account_id=teacher.id,
        auth_session_id=parent.id, status=ImpersonationStatus.ACTIVE, reason="fixture support",
        started_at=now, expires_at=now + timedelta(minutes=15))
    membership = ClubMembership(club_id=club.id, account_id=teacher.id, role=ClubRole.TEACHER_REP,
                                status=MembershipStatus.ENDED, start_date=historical.start_date, end_date=historical.end_date)
    activity = Activity(club_id=club.id, created_by=student.id)
    db.add_all([impersonation, membership, activity])
    db.flush()
    revision = ActivityRevision(activity_id=activity.id, revision_number=1, title="Historical activity",
        description="Preserved business record", category="STEM", activity_date=historical.end_date,
        status=ActivityRevisionStatus.APPROVED, is_published=True)
    db.add(revision)
    db.flush()
    review = ActivityReview(revision_id=revision.id, actor_account_id=admin.id, represented_account_id=teacher.id,
        impersonation_session_id=impersonation.id, review_mode=ReviewMode.IMPERSONATION, decision=ActivityReviewDecision.APPROVED)
    db.add(review)
    path = add_attachment(db, storage, Publication(club.id, activity.id, revision.id))
    db.flush()
    return {"school": school.id, "other_school": other.id, "year": historical.id, "teacher": teacher.id,
            "admin": admin.id, "membership": membership.id, "review": review.id, "impersonation": impersonation.id,
            "tokens": {parent.id: parent.token_hash, old.id: old.token_hash},
            "attachment": UUID(path.rsplit("/", 1)[-1]), "revision": revision.id}


def test_backup_restore_and_corruption_protection(disposable_databases, tmp_path: Path, monkeypatch):
    source_name, target_name = disposable_databases
    root = tmp_path.resolve()
    root.chmod(0o700)
    source = create_engine(database.database_url.set(database=source_name))
    private_store = LocalAttachmentStorage(root / "source")
    backup_root = root / "backups"
    backup_root.mkdir(mode=0o700)
    restore_root = root / "restored"
    target = create_engine(database.database_url.set(database=target_name))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(database, "engine", source)
            command.upgrade(Config("alembic.ini"), "fe7b134d822d")
            operation = uuid4()
            payload = {"title": "Old request", "description": "Keep only its fingerprint"}
            with Session(source) as db, db.begin():
                owner = Account(account_type=AccountType.PLATFORM_OWNER, full_name="Fixture", username="fixture",
                                email="fixture@example.test", password_hash="fixture-only", is_active=True)
                db.add(owner)
                db.flush()
                db.add(AuditLog(id=operation, actor_account_id=owner.id, action="ACTIVITY_DRAFT_CREATED",
                    entity_type="activity", occurred_at=database_now(db) - timedelta(days=30), details={"payload": payload}))
            command.upgrade(Config("alembic.ini"), "head")

        with Session(source) as db, db.begin():
            fixture = _seed_history(db, private_store)
        with exclusive_maintenance(source) as db:
            cleanup_history(db, limit=100)
            assert db.get(AuditLog, operation) is None
            receipt = find_receipt(db, operation, "ACTIVITY_DRAFT_CREATED", payload, ActivityError)
            assert receipt is not None and len(receipt.fingerprint) == 32
            db.rollback()
            bind_storage(db, private_store, "test")
            archive = create_backup(db, private_store, backup_root, "test")

        manifest = verify_backup(archive)
        assert manifest.attachment_count == 1
        assert restore_backup(archive, target_name, restore_root) == manifest
        with pytest.raises(BackupError, match="already exists"):
            restore_backup(archive, target_name, root / "second-restore")
        with pytest.raises(BackupError, match="new clubos_restore_"):
            restore_backup(archive, "clubos_test", root / "existing-development")

        restored_storage = LocalAttachmentStorage(restore_root)
        with Session(target) as db:
            assert db.get(AcademicYear, fixture["year"]).status == AcademicYearStatus.CLOSED
            member = db.get(ClubMembership, fixture["membership"])
            assert member.status == MembershipStatus.ENDED and member.account_id == fixture["teacher"]
            assert db.get(Club, member.club_id).school_id == fixture["school"] != fixture["other_school"]
            review = db.get(ActivityReview, fixture["review"])
            assert (review.review_mode, review.actor_account_id, review.represented_account_id) == (
                ReviewMode.IMPERSONATION, fixture["admin"], fixture["teacher"],
            )
            assert review.impersonation_session_id == fixture["impersonation"]
            for session_id, original_token in fixture["tokens"].items():
                session = db.get(AuthSession, session_id)
                assert session.revoked_at is not None and session.token_hash != original_token
            impersonation = db.get(ImpersonationSession, fixture["impersonation"])
            assert impersonation.status == ImpersonationStatus.EXPIRED and impersonation.end_reason == "RESTORE_INVALIDATED"
            binding = validate_storage_pairing(db, restored_storage, "test")
            assert binding.storage_identity != manifest.storage_identity
            with pytest.raises(MaintenanceError, match="not paired"):
                validate_storage_pairing(db, private_store, "test")
            attachment = db.get(ActivityAttachment, fixture["attachment"])
            assert attachment.scan_passed_at is not None
            with restored_storage.get(attachment.storage_key) as stream:
                assert stream.read(len(sample_pdf()) + 1) == sample_pdf()

        filename = attachment_object_key(fixture["revision"], fixture["attachment"])
        copy = LocalAttachmentStorage(archive / "objects")
        copy.put(filename, b"changed fixture", "application/pdf")
        with pytest.raises(StorageError, match="does not match"):
            verify_backup(archive)
        copy.put(filename, sample_pdf(), "application/pdf")
        with (archive / "database.dump").open("ab") as stream:
            stream.write(b"changed fixture")
        with pytest.raises(BackupError, match="checksum"):
            verify_backup(archive)

        with exclusive_maintenance(source) as db:
            monkeypatch.setattr(backups, "database_now", lambda _: manifest.created_at + timedelta(days=29))
            assert prune_backups(db, private_store, backup_root, "test", 1, False) == 0
            monkeypatch.setattr(backups, "database_now", lambda _: manifest.created_at + timedelta(days=30))
            assert prune_backups(db, private_store, backup_root, "test", 1, True) == 1 and archive.is_dir()
            assert prune_backups(db, private_store, backup_root, "test", 1, False) == 1 and not archive.exists()
    finally:
        source.dispose()
        target.dispose()
