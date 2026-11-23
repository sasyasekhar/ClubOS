from collections.abc import Iterator
from functools import partial
from typing import BinaryIO
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.models.account import Account
from app.models.enums import AccountType
from app.schemas.academic_year import AcademicYearClosureReadiness, AcademicYearCreate, AcademicYearResponse
from app.security.permissions import require_roles
from app.services.academic_year_closure import check_closure_readiness
from app.services.academic_year_exports import EXPORT_COOLDOWN_SECONDS, open_academic_year_export
from app.services.academic_years import (
    AcademicYearError,
    create_academic_year,
    list_academic_years,
)


router = APIRouter(prefix="/academic-years", tags=["Academic Years"])
require_school_admin = require_roles(AccountType.SCHOOL_ADMIN)
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
