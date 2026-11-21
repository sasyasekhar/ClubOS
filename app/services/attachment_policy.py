
from dataclasses import dataclass
from pathlib import PurePath
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.enums import ActivityRevisionStatus
from app.services.activity_workflow import authorized_revision
from app.services.activity_drafts import ActivityError


MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

ALLOWED_TYPES = {
    ".pdf": ("application/pdf", b"%PDF-"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
}


class AttachmentValidationError(Exception):
    pass


@dataclass(frozen=True)
class ValidatedAttachment:
    file_name: str
    file_type: str
    size: int


def validate_attachment(
    filename: str,
    content_type: str,
    content: bytes,
) -> ValidatedAttachment:

    if (
        not filename
        or len(filename) > 255
        or "/" in filename
        or "\\" in filename
        or any(ord(char) < 32 for char in filename)
    ):
        raise AttachmentValidationError("Invalid filename.")

    extension = PurePath(filename).suffix.lower()

    if extension not in ALLOWED_TYPES:
        raise AttachmentValidationError("File type not allowed.")

    expected_type, signature = ALLOWED_TYPES[extension]

    if content_type != expected_type:
        raise AttachmentValidationError("File type mismatch.")

    if not content or len(content) > MAX_FILE_SIZE:
        raise AttachmentValidationError("Invalid file size.")

    if not content.startswith(signature):
        raise AttachmentValidationError("Invalid file signature.")

    return ValidatedAttachment(
        file_name=filename,
        file_type=expected_type,
        size=len(content),
    )


def ensure_upload_allowed(
    db: Session,
    student: Account,
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
):
    revision = authorized_revision(
        db,
        student,
        club_id,
        activity_id,
        revision_id,
    )

    if revision.status != ActivityRevisionStatus.DRAFT:
        raise ActivityError(
            409,
            "Attachments can only be added to draft revisions.",
        )

    return revision
