import hashlib
import logging
import re
from dataclasses import dataclass
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy import select
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
from app.services.activity_drafts import ActivityError
from app.services.attachment_policy import (
    ALLOWED_TYPES,
    MAX_FILE_SIZE,
    AttachmentValidationError,
    ensure_upload_allowed,
    is_safe_filename,
    validate_attachment,
)
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
) -> ActivityAttachmentResponse:
    storage_key_to_remove = None
    try:
        ensure_upload_allowed(db, student, club_id, activity_id, revision_id)
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
            result = _upload_response(existing)
            db.rollback()
            return result

        validated = validate_attachment(filename, content_type, content)
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

        attachment = db.scalar(
            select(ActivityAttachment).where(
                ActivityAttachment.id == attachment_id,
                ActivityAttachment.revision_id == revision.id,
            )
        )
        if attachment is None or not has_managed_object(attachment):
            raise ActivityError(404, "Attachment not found.")

        if (
            not is_safe_filename(attachment.file_name)
            or attachment.file_type not in DOWNLOAD_TYPES
        ):
            raise StorageError("Invalid attachment metadata.")

        # Hold a shared revision lock until we have a complete, bounded snapshot.
        # The response can then be sent without holding database or storage resources.
        expected_size = attachment.size_bytes
        assert expected_size is not None
        with storage.get(attachment.storage_key) as source:
            content = _read_bounded(source, min(expected_size, MAX_FILE_SIZE) + 1)
        if (
            len(content) != expected_size
            or hashlib.sha256(content).hexdigest() != attachment.content_sha256
        ):
            raise StorageError("Attachment content does not match metadata.")

        return AttachmentDownload(attachment.file_name, attachment.file_type, content)
    except ObjectMissing as exc:
        logger.warning("Attachment object missing for attachment %s", attachment_id)
        raise ActivityError(404, "Attachment not found.") from exc
    except (SQLAlchemyError, StorageError, OSError) as exc:
        logger.error(
            "Attachment download failed for attachment %s (%s)", attachment_id, type(exc).__name__
        )
        raise ActivityError(503, "Attachment download is temporarily unavailable.") from exc
    finally:
        db.rollback()


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
        db.scalar(
            select(ActivityRevision.id)
            .where(ActivityRevision.id == UUID(hex=match[1]))
            .with_for_update()
        )
        # This statement starts after acquiring the revision lock, so it sees
        # any upload that committed while reconciliation was waiting.
        referenced = db.scalar(
            select(ActivityAttachment.id).where(ActivityAttachment.storage_key == key).limit(1)
        )
        if referenced is not None:
            return False
        storage.delete(key)
        return True
    finally:
        db.rollback()
