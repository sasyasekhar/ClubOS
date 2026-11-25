import hashlib
import logging
import re
from dataclasses import dataclass
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.activity import Activity
from app.models.activity_attachment import ActivityAttachment
from app.models.activity_revision import ActivityRevision
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.enums import AccountType, ClubRole, MembershipStatus
from app.schemas.activity import ActivityAttachmentResponse
from app.services.academic_year_write_guard import require_completion_window_open
from app.services.activity_drafts import ActivityError
from app.services.activity_workflow import authorized_revision, previous_publication, recovery_deadline
from app.services.attachment_policy import (
    ALLOWED_TYPES,
    MAX_FILE_SIZE,
    AttachmentValidationError,
    is_safe_filename,
    require_uploadable_revision,
    validate_attachment,
)
from app.services.attachment_scanning import AttachmentScanner, ScannerUnavailable, ScanVerdict
from app.services.sessions import database_now
from app.services.storage import AttachmentStorage, ObjectMissing, StorageError


logger = logging.getLogger(__name__)
OBJECT_PREFIX = "activity-attachments"
OBJECT_KEY = re.compile(rf"{OBJECT_PREFIX}/([0-9a-f]{{32}})/[0-9a-f]{{32}}")
REP_ROLES = {
    AccountType.STUDENT: ClubRole.STUDENT_REP,
    AccountType.TEACHER: ClubRole.TEACHER_REP,
}
DOWNLOAD_TYPES = {media_type for media_type, _ in ALLOWED_TYPES.values()}


@dataclass(frozen=True)
class AttachmentDownload:
    file_name: str
    file_type: str
    content: bytes


def attachment_object_key(revision_id: UUID, attachment_id: UUID) -> str:
    return f"{OBJECT_PREFIX}/{revision_id.hex}/{attachment_id.hex}"


def has_managed_object(attachment: ActivityAttachment) -> bool:
    return (
        attachment.size_bytes is not None
        and attachment.storage_key == attachment_object_key(attachment.revision_id, attachment.id)
    )


def attachment_can_be_copied(attachment: ActivityAttachment) -> bool:
    return (attachment.scan_passed_at is not None and has_managed_object(attachment)
            and is_safe_filename(attachment.file_name) and attachment.file_type in DOWNLOAD_TYPES)


def lock_new_revision_objects(db: Session, revision_id: UUID) -> None:
    # A new revision is invisible to other transactions until its first commit.
    key = func.hashtextextended(f"clubos:revision-objects:{revision_id}", 0)
    db.execute(select(func.pg_advisory_xact_lock(key)))


def _check_student_previous_download(db: Session, viewer: Account, revision: ActivityRevision) -> None:
    if viewer.account_type != AccountType.STUDENT or revision.superseded_at is None:
        return
    latest = previous_publication(revision.activity_id).with_only_columns(ActivityRevision.id)
    if db.scalar(latest) != revision.id or database_now(db) >= recovery_deadline(revision.superseded_at):
        raise ActivityError(404, "Attachment not found.")


def upload_attachment(
    db: Session,
    storage: AttachmentStorage,
    student: Account,
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    idempotency_key: UUID,
    filename: str,
    content_type: str,
    file: BinaryIO,
    *,
    scanner: AttachmentScanner | None = None,
) -> ActivityAttachmentResponse:
    storage_key_to_remove = None
    try:
        revision, year = authorized_revision(db, student, club_id, activity_id, revision_id)
        require_uploadable_revision(revision)
        content = file.read(MAX_FILE_SIZE + 1)
        if len(content) > MAX_FILE_SIZE:
            raise ActivityError(413, "Attachment exceeds 10 MiB.")
        content_hash = hashlib.sha256(content).hexdigest()

        existing = db.scalar(
            select(ActivityAttachment).where(
                ActivityAttachment.revision_id == revision_id,
                ActivityAttachment.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            # An identical digest, filename, and media type replay the bytes
            # that already passed validation.
            if (
                existing.file_name != filename
                or existing.file_type != content_type.lower()
                or existing.size_bytes != len(content)
                or existing.content_sha256 != content_hash
            ):
                raise ActivityError(409, "Idempotency key conflict.")
            if existing.scan_passed_at is None:
                raise ActivityError(409, "Existing attachment has not passed scanning.")
            result = _upload_response(existing)
            db.rollback()
            return result

        require_completion_window_open(db, year, ActivityError)
        validated = validate_attachment(filename, content_type, content)
        _require_clean_scan(scanner, content)
        scan_passed_at = require_completion_window_open(db, year, ActivityError)
        attachment_id = uuid4()
        attachment = ActivityAttachment(
            id=attachment_id,
            revision_id=revision_id,
            file_name=validated.file_name,
            file_type=validated.file_type,
            storage_key=attachment_object_key(revision_id, attachment_id),
            idempotency_key=idempotency_key,
            size_bytes=validated.size,
            content_sha256=content_hash,
            scan_passed_at=scan_passed_at,
        )
        db.add(attachment)
        # Reject immediate database constraints before touching storage. Every
        # attempt owns a new key, while the revision lock serializes retries.
        db.flush()
        result = _upload_response(attachment)
        storage_key_to_remove = attachment.storage_key
        storage.put(attachment.storage_key, content, attachment.file_type)

        # A failed COMMIT acknowledgement can mean the row was committed.
        # Reconciliation checks under the same revision lock before deleting.
        storage_key_to_remove = None
        db.commit()
        return result
    except AttachmentValidationError as exc:
        db.rollback()
        raise ActivityError(400, str(exc)) from exc
    except ActivityError:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint == "uq_attachment_revision_idempotency":
            raise ActivityError(409, "Idempotency key conflict.") from exc
        logger.error("Attachment metadata failed a database constraint for revision %s", revision_id)
        raise ActivityError(503, "Attachment upload is temporarily unavailable.") from exc
    except (SQLAlchemyError, StorageError, OSError) as exc:
        db.rollback()
        logger.error(
            "Attachment upload failed for revision %s (%s)", revision_id, type(exc).__name__
        )
        if storage_key_to_remove is not None:
            try:
                storage.delete(storage_key_to_remove)
            except StorageError:
                logger.error(
                    "Attachment upload needs orphan reconciliation for revision %s", revision_id
                )
        raise ActivityError(503, "Attachment upload is temporarily unavailable.") from exc


def _require_clean_scan(scanner: AttachmentScanner | None, content: bytes) -> None:
    if scanner is None:
        raise ActivityError(503, "Attachment scanner is unavailable.")
    try:
        verdict = scanner.scan(content)
    except (ScannerUnavailable, OSError) as exc:
        raise ActivityError(503, "Attachment scanner is unavailable.") from exc
    if verdict is ScanVerdict.UNSAFE:
        raise ActivityError(422, "Attachment did not pass the security scan.")
    if verdict is not ScanVerdict.CLEAN:
        raise ActivityError(503, "Attachment scanner is unavailable.")


def _upload_response(attachment: ActivityAttachment) -> ActivityAttachmentResponse:
    return ActivityAttachmentResponse(
        id=attachment.id,
        revision_id=attachment.revision_id,
        file_name=attachment.file_name,
        file_type=attachment.file_type,
    )


def download_attachment(
    db: Session,
    storage: AttachmentStorage,
    viewer: Account,
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    attachment_id: UUID,
) -> AttachmentDownload:
    try:
        return load_activity_attachment(db, storage, viewer, club_id, activity_id, revision_id, attachment_id)
    finally:
        db.rollback()


def load_activity_attachment(
    db: Session,
    storage: AttachmentStorage,
    viewer: Account,
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    attachment_id: UUID,
) -> AttachmentDownload:
    """Read a bounded snapshot while leaving the caller's transaction open."""
    try:
        if (
            not viewer.is_active
            or viewer.school_id is None
            or viewer.account_type not in (*REP_ROLES, AccountType.SCHOOL_ADMIN)
        ):
            raise ActivityError(403, "Permission denied.")

        revision = db.scalar(
            select(ActivityRevision)
            .join(Activity, Activity.id == ActivityRevision.activity_id)
            .join(Club, Club.id == Activity.club_id)
            .where(
                Club.id == club_id,
                Club.school_id == viewer.school_id,
                Activity.id == activity_id,
                ActivityRevision.id == revision_id,
                ActivityRevision.content_purged_at.is_(None),
            )
            .with_for_update(read=True, of=ActivityRevision)
        )
        if revision is None:
            raise ActivityError(404, "Attachment not found.")

        if not revision.is_published and viewer.account_type != AccountType.SCHOOL_ADMIN:
            membership = db.scalar(
                select(ClubMembership.id).where(
                    ClubMembership.club_id == club_id,
                    ClubMembership.account_id == viewer.id,
                    ClubMembership.role == REP_ROLES[viewer.account_type],
                    ClubMembership.status == MembershipStatus.ACTIVE,
                )
            )
            if membership is None:
                raise ActivityError(404, "Attachment not found.")

        _check_student_previous_download(db, viewer, revision)

        attachment = db.scalar(
            select(ActivityAttachment).where(
                ActivityAttachment.id == attachment_id,
                ActivityAttachment.revision_id == revision.id,
            )
        )
        if attachment is None or attachment.scan_passed_at is None or not has_managed_object(attachment):
            raise ActivityError(404, "Attachment not found.")

        if not attachment_can_be_copied(attachment):
            raise StorageError("Invalid attachment metadata.")

        # Hold a shared revision lock until we have a complete, bounded snapshot.
        # The response can then be sent without holding database or storage resources.
        content = read_verified_object(storage, attachment.storage_key, attachment.size_bytes, attachment.content_sha256)
        _check_student_previous_download(db, viewer, revision)
        return AttachmentDownload(attachment.file_name, attachment.file_type, content)
    except ObjectMissing as exc:
        logger.warning("Attachment object missing for attachment %s", attachment_id)
        raise ActivityError(404, "Attachment not found.") from exc
    except (SQLAlchemyError, StorageError, OSError) as exc:
        logger.error(
            "Attachment download failed for attachment %s (%s)", attachment_id, type(exc).__name__
        )
        raise ActivityError(503, "Attachment download is temporarily unavailable.") from exc


def read_verified_object(
    storage: AttachmentStorage, key: str, expected_size: int | None, expected_hash: str | None,
) -> bytes:
    if expected_size is None or not 1 <= expected_size <= MAX_FILE_SIZE or expected_hash is None:
        raise StorageError("Attachment metadata cannot be verified.")
    with storage.get(key) as source:
        content = _read_bounded(source, expected_size + 1)
    if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_hash:
        raise StorageError("Attachment content does not match metadata.")
    return content


def _read_bounded(source: BinaryIO, limit: int) -> bytes:
    content = source.read(limit)
    if len(content) == limit or not (additional := source.read(limit - len(content))):
        return content

    # Remote streams can return a partial read before reaching EOF.
    buffer = bytearray(content)
    buffer.extend(additional)
    while len(buffer) < limit and (chunk := source.read(limit - len(buffer))):
        buffer.extend(chunk)
    return bytes(buffer)


def reconcile_orphaned_upload(db: Session, storage: AttachmentStorage, key: str) -> bool:
    """Delete one unreferenced managed object from a private storage inventory."""
    match = OBJECT_KEY.fullmatch(key)
    if match is None:
        return False

    try:
        revision_id = UUID(hex=match[1])
        existing = db.scalar(
            select(ActivityRevision.id)
            .where(ActivityRevision.id == revision_id)
            .with_for_update()
        )
        if existing is None:
            lock_new_revision_objects(db, revision_id)
        # Read references after waiting for the upload or initial revision commit.
        referenced = db.scalar(
            select(ActivityAttachment.id).where(ActivityAttachment.storage_key == key).limit(1)
        )
        if referenced is not None:
            return False
        storage.delete(key)
        return True
    finally:
        db.rollback()
