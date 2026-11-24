from collections.abc import Iterator
from functools import partial
from typing import BinaryIO
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.models.account import Account
from app.models.enums import AccountType
from app.schemas.academic_year import (
    AcademicYearClosureReadiness,
    AcademicYearCreate,
    AcademicYearResponse,
    ClosureConfirmation,
    ClosureRequestResponse,
    ClosureTeacherDesignation,
    ClosureTeacherResponse,
)
from app.schemas.academic_year_archive import AcademicYearArchiveRequest, ArchivedAcademicYearResponse
from app.schemas.academic_year_rollover import ClubRolloverRequest, ClubRolloverResponse, RolloverCandidatesPage
from app.security.permissions import require_roles
from app.services.academic_year_archiving import archive_academic_year
from app.services.academic_year_closure import check_closure_readiness
from app.services.academic_year_closure_workflow import (
    confirm_year_closure,
    get_closure_request,
    get_closure_teacher,
    request_year_closure,
    set_closure_teacher,
)
from app.services.academic_year_exports import EXPORT_COOLDOWN_SECONDS, open_academic_year_export
from app.services.academic_year_rollover import list_rollover_candidates, rollover_clubs
from app.services.academic_years import (
    AcademicYearError,
    create_academic_year,
    list_academic_years,
)


router = APIRouter(prefix="/academic-years", tags=["Academic Years"])
require_school_admin = require_roles(AccountType.SCHOOL_ADMIN)
require_teacher = require_roles(AccountType.TEACHER)
CLOSURE_HEADERS = {"Cache-Control": "private, no-store"}
EXPORT_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


@router.post("", status_code=status.HTTP_201_CREATED)
def register_year(
    payload: AcademicYearCreate,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    admin: Account = Depends(require_school_admin),
    db: Session = Depends(get_db),
) -> AcademicYearResponse:
    try:
        return create_academic_year(db, admin, payload, idempotency_key)
    except AcademicYearError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.get("")
def get_academic_years(
    response: Response,
    admin: Account = Depends(require_school_admin),
    db: Session = Depends(get_db),
) -> list[AcademicYearResponse]:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return list_academic_years(db, admin)
    except AcademicYearError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.get("/closure-teacher")
def view_closure_teacher(
    response: Response,
    admin: Account = Depends(require_school_admin),
    db: Session = Depends(get_db),
) -> ClosureTeacherResponse:
    response.headers.update(CLOSURE_HEADERS)
    try:
        return get_closure_teacher(db, admin)
    except AcademicYearError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=CLOSURE_HEADERS) from exc


@router.put("/closure-teacher")
def designate_closure_teacher(
    payload: ClosureTeacherDesignation,
    response: Response,
    admin: Account = Depends(require_school_admin),
    db: Session = Depends(get_db),
) -> ClosureTeacherResponse:
    response.headers.update(CLOSURE_HEADERS)
    try:
        return set_closure_teacher(db, admin, payload.teacher_account_id)
    except AcademicYearError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=CLOSURE_HEADERS) from exc


@router.get("/{year_id}/closure-readiness")
def get_closure_readiness(
    year_id: UUID,
    response: Response,
    admin: Account = Depends(require_school_admin),
    db: Session = Depends(get_db),
) -> AcademicYearClosureReadiness:
    response.headers["Cache-Control"] = "private, no-store"
    try:
        return check_closure_readiness(db, admin, year_id)
    except AcademicYearError as exc:
        raise HTTPException(
            exc.status_code, exc.detail, headers={"Cache-Control": "private, no-store"}
        ) from exc


@router.get("/{year_id}/closure-request")
def view_closure_request(
    year_id: UUID,
    response: Response,
    admin: Account = Depends(require_school_admin),
    db: Session = Depends(get_db),
) -> ClosureRequestResponse | None:
    response.headers.update(CLOSURE_HEADERS)
    try:
        return get_closure_request(db, admin, year_id)
    except AcademicYearError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=CLOSURE_HEADERS) from exc


@router.post("/{year_id}/closure-request", status_code=status.HTTP_201_CREATED)
def initiate_closure(
    year_id: UUID,
    response: Response,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    teacher: Account = Depends(require_teacher),
    db: Session = Depends(get_db),
) -> ClosureRequestResponse:
    response.headers.update(CLOSURE_HEADERS)
    try:
        return request_year_closure(db, teacher, year_id, idempotency_key)
    except AcademicYearError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=CLOSURE_HEADERS) from exc


@router.post("/{year_id}/confirm-closure")
def confirm_closure(
    year_id: UUID,
    payload: ClosureConfirmation,
    response: Response,
    admin: Account = Depends(require_school_admin),
    db: Session = Depends(get_db),
) -> ClosureRequestResponse:
    response.headers.update(CLOSURE_HEADERS)
    try:
        return confirm_year_closure(db, admin, year_id, payload.request_id)
    except AcademicYearError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=CLOSURE_HEADERS) from exc


@router.post("/{year_id}/archive")
def archive_year(
    year_id: UUID,
    payload: AcademicYearArchiveRequest,
    response: Response,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    admin: Account = Depends(require_school_admin),
    db: Session = Depends(get_db),
) -> ArchivedAcademicYearResponse:
    response.headers.update(CLOSURE_HEADERS)
    try:
        return archive_academic_year(db, admin, year_id, idempotency_key, export_saved=payload.export_saved)
    except AcademicYearError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=CLOSURE_HEADERS) from exc


@router.get("/{target_year_id}/rollover-candidates")
def get_rollover_candidates(
    target_year_id: UUID,
    response: Response,
    limit: int = Query(default=20, ge=1, le=50),
    after: UUID | None = None,
    admin: Account = Depends(require_school_admin),
    db: Session = Depends(get_db),
) -> RolloverCandidatesPage:
    response.headers.update(CLOSURE_HEADERS)
    try:
        return list_rollover_candidates(db, admin, target_year_id, limit=limit, after=after)
    except AcademicYearError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=CLOSURE_HEADERS) from exc


@router.post("/{target_year_id}/rollover-clubs", status_code=status.HTTP_201_CREATED)
def create_rollover_clubs(
    target_year_id: UUID,
    payload: ClubRolloverRequest,
    response: Response,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    admin: Account = Depends(require_school_admin),
    db: Session = Depends(get_db),
) -> ClubRolloverResponse:
    response.headers.update(CLOSURE_HEADERS)
    try:
        return rollover_clubs(db, admin, target_year_id, payload, idempotency_key)
    except AcademicYearError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=CLOSURE_HEADERS) from exc


def _export_archive(
    year_id: UUID,
    admin: Account = Depends(require_school_admin),
    db: Session = Depends(get_db),
) -> Iterator[tuple[BinaryIO, int]]:
    try:
        with open_academic_year_export(db, admin, year_id) as archive:
            yield archive
    except AcademicYearError as exc:
        headers = dict(EXPORT_HEADERS)
        if exc.status_code == 429:
            headers["Retry-After"] = str(EXPORT_COOLDOWN_SECONDS)
        raise HTTPException(exc.status_code, exc.detail, headers=headers) from exc


@router.post(
    "/{year_id}/export",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}},
)
def export_academic_year(
    year_id: UUID,
    archive: tuple[BinaryIO, int] = Depends(_export_archive),
) -> StreamingResponse:
    output, length = archive
    return StreamingResponse(
        iter(partial(output.read, 64 * 1024), b""),
        media_type="application/zip",
        headers={
            **EXPORT_HEADERS,
            "Content-Disposition": f'attachment; filename="academic-year-{year_id}.zip"',
            "Content-Length": str(length),
        },
    )
