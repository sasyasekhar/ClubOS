
from collections.abc import AsyncGenerator
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from python_multipart.multipart import parse_options_header
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.config.database import get_db
from app.models.account import Account
from app.models.enums import AccountType
from app.routes.auth import require_account
from app.schemas.activity import (
    ActivityAttachmentResponse,
    ActivityDraftCreate,
    ActivityDraftResponse,
    ActivityReviewRequest,
    ActivityReviewResponse,
    PreviousActivityVersion,
    RestorePreviousVersionRequest,
)
from app.schemas.dashboard import DashboardRevisionDetail, RevisionDetailQuery
from app.security.permissions import require_roles
from app.security.requests import MAX_ATTACHMENT_REQUEST_SIZE
from app.services.activity_attachments import AttachmentDownload, download_attachment, upload_attachment
from app.services.activity_corrections import create_corrected_draft
from app.services.activity_drafts import (
    ActivityError,
    create_activity_draft,
)
from app.services.activity_replacements import create_replacement_draft
from app.services.activity_recovery import preview_previous_version, restore_previous_version
from app.services.activity_review import review_activity
from app.services.activity_workflow import (
    edit_activity_draft,
    submit_activity_draft,
)
from app.services.attachment_scanning import AttachmentScanner, get_attachment_scanner
from app.services.dashboard import get_dashboard_revision
from app.services.storage import AttachmentStorage, get_attachment_storage

router = APIRouter(
    prefix="/clubs",
    tags=["Activities"],
)

ACTIVITY_PATH = "/{club_id}/activities/{activity_id}"
REVISION_PATH = ACTIVITY_PATH + "/revisions/{revision_id}"
ATTACHMENTS_PATH = REVISION_PATH + "/attachments"
PRIVATE_DOWNLOAD_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}


@router.get(ACTIVITY_PATH + "/previous-version")
def get_previous_version(
    club_id: UUID, activity_id: UUID, response: Response,
    student: Account = Depends(require_roles(AccountType.STUDENT)), db: Session = Depends(get_db),
) -> PreviousActivityVersion:
    response.headers.update(PRIVATE_DOWNLOAD_HEADERS)
    try:
        return preview_previous_version(db, student, club_id, activity_id)
    except ActivityError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_DOWNLOAD_HEADERS) from exc


@router.post(ACTIVITY_PATH + "/restore-previous-version", status_code=status.HTTP_201_CREATED,
             response_model=ActivityDraftResponse)
def restore_previous_activity_version(
    club_id: UUID, activity_id: UUID, payload: RestorePreviousVersionRequest, response: Response,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    student: Account = Depends(require_roles(AccountType.STUDENT)), db: Session = Depends(get_db),
    storage: AttachmentStorage = Depends(get_attachment_storage),
):
    response.headers.update(PRIVATE_DOWNLOAD_HEADERS)
    try:
        return restore_previous_version(db, storage, student, club_id, activity_id, payload.expected_revision_id, idempotency_key)
    except ActivityError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_DOWNLOAD_HEADERS) from exc


@router.get(REVISION_PATH)
def view_activity_revision(
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    query: Annotated[RevisionDetailQuery, Query()],
    response: Response,
    viewer: Account = Depends(require_roles(AccountType.STUDENT, AccountType.TEACHER)),
    db: Session = Depends(get_db),
) -> DashboardRevisionDetail:
    response.headers["Cache-Control"] = PRIVATE_DOWNLOAD_HEADERS["Cache-Control"]
    try:
        return get_dashboard_revision(db, viewer, club_id, activity_id, revision_id, query)
    except ActivityError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_DOWNLOAD_HEADERS) from exc


@router.post(
    "/{club_id}/activities",
    response_model=ActivityDraftResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_draft(
    club_id: UUID,
    payload: ActivityDraftCreate,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    student: Account = Depends(
        require_roles(AccountType.STUDENT)
    ),
    db: Session = Depends(get_db),
):
    try:
        return create_activity_draft(
            db,
            student,
            club_id,
            payload,
            idempotency_key,
        )

    except ActivityError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

@router.put(
    "/{club_id}/activities/{activity_id}/revisions/{revision_id}",
    response_model=ActivityDraftResponse,
)
def update_draft(
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    payload: ActivityDraftCreate,
    student: Account = Depends(
        require_roles(AccountType.STUDENT)
    ),
    db: Session = Depends(get_db),
):
    try:
        return edit_activity_draft(
            db, student, club_id, activity_id, revision_id, payload
        )
    except ActivityError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc


@router.post(
    "/{club_id}/activities/{activity_id}/revisions/{revision_id}/submit",
    response_model=ActivityDraftResponse,
)
def submit_draft(
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    student: Account = Depends(
        require_roles(AccountType.STUDENT)
    ),
    db: Session = Depends(get_db),
):
    try:
        return submit_activity_draft(
            db, student, club_id, activity_id, revision_id
        )
    except ActivityError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

@router.post(
    "/{club_id}/activities/{activity_id}/revisions/{revision_id}/review",
    response_model=ActivityReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
def review_activity_revision(
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    payload: ActivityReviewRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    teacher: Account = Depends(
        require_roles(AccountType.TEACHER)
    ),
    db: Session = Depends(get_db),
):
    try:
        return review_activity(
            db,
            teacher,
            club_id,
            activity_id,
            revision_id,
            payload,
            idempotency_key,
        )
    except ActivityError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc


@router.post(
    "/{club_id}/activities/{activity_id}/revisions/{revision_id}/correct",
    response_model=ActivityDraftResponse,
    status_code=status.HTTP_201_CREATED,
)
def correct_activity_revision(
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    payload: ActivityDraftCreate,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    student: Account = Depends(
        require_roles(AccountType.STUDENT)
    ),
    db: Session = Depends(get_db),
):
    try:
        return create_corrected_draft(
            db,
            student,
            club_id,
            activity_id,
            revision_id,
            payload,
            idempotency_key,
        )
    except ActivityError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

@router.post(
    "/{club_id}/activities/{activity_id}/revisions/{revision_id}/replace",
    response_model=ActivityDraftResponse,
    status_code=status.HTTP_201_CREATED,
)
def replace_activity_revision(
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    payload: ActivityDraftCreate,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    student: Account = Depends(require_roles(AccountType.STUDENT)),
    db: Session = Depends(get_db),
):
    try:
        return create_replacement_draft(
            db, student, club_id, activity_id,
            revision_id, payload, idempotency_key,
        )
    except ActivityError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc


class AttachmentRequestTooLarge(MultiPartException):
    pass


async def bounded_attachment_body(request: Request) -> AsyncGenerator[bytes, None]:
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_ATTACHMENT_REQUEST_SIZE:
            raise AttachmentRequestTooLarge("Attachment request is too large.")
        yield chunk


@router.post(
    ATTACHMENTS_PATH,
    response_model=ActivityAttachmentResponse,
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {"file": {"type": "string", "format": "binary"}},
                        "required": ["file"],
                    }
                }
            },
        }
    },
)
async def add_activity_attachment(
    request: Request,
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    student: Account = Depends(require_roles(AccountType.STUDENT)),
    db: Session = Depends(get_db),
    storage: AttachmentStorage = Depends(get_attachment_storage),
    scanner: AttachmentScanner | None = Depends(get_attachment_scanner),
) -> ActivityAttachmentResponse:
    media_type, _ = parse_options_header(request.headers.get("content-type", ""))
    if media_type != b"multipart/form-data":
        raise HTTPException(415, "Expected a multipart file upload.")
    try:
        form = await MultiPartParser(
            request.headers, bounded_attachment_body(request), max_files=1, max_fields=0
        ).parse()
    except AttachmentRequestTooLarge as exc:
        raise HTTPException(413, "Attachment request is too large.") from exc
    except MultiPartException as exc:
        raise HTTPException(400, "Invalid attachment upload.") from exc

    try:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(422, "A file part is required.")
        # The multipart parser can strip Windows paths from filenames.
        dispositions = file.headers.getlist("content-disposition")
        if len(dispositions) != 1 or "\\" in dispositions[0]:
            raise HTTPException(400, "Invalid filename.")
        return await run_in_threadpool(
            upload_attachment,
            db,
            storage,
            student,
            club_id,
            activity_id,
            revision_id,
            idempotency_key,
            file.filename or "",
            file.content_type or "",
            file.file,
            scanner=scanner,
        )
    except ActivityError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    finally:
        await form.close()


@router.get(ATTACHMENTS_PATH + "/{attachment_id}", response_class=Response)
def get_activity_attachment(
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    attachment_id: UUID,
    viewer: Account = Depends(require_account),
    db: Session = Depends(get_db),
    storage: AttachmentStorage = Depends(get_attachment_storage),
) -> Response:
    try:
        attachment = download_attachment(
            db, storage, viewer, club_id, activity_id, revision_id, attachment_id
        )
    except ActivityError as exc:
        raise HTTPException(
            exc.status_code, exc.detail, headers=PRIVATE_DOWNLOAD_HEADERS
        ) from exc

    return attachment_download_response(attachment)


def attachment_download_response(attachment: AttachmentDownload) -> Response:
    fallback = "".join(
        char if char.isascii() and (char.isalnum() or char in " ._-") else "_"
        for char in attachment.file_name
    )
    encoded = quote(attachment.file_name, safe="")
    return Response(
        attachment.content,
        media_type=attachment.file_type,
        headers={
            **PRIVATE_DOWNLOAD_HEADERS,
            "Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}",
        },
    )
