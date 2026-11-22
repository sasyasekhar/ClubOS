from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.models.account import Account
from app.models.enums import AccountType
from app.schemas.academic_year import AcademicYearCreate, AcademicYearResponse
from app.security.permissions import require_roles
from app.services.academic_years import (
    AcademicYearError,
    create_academic_year,
    list_academic_years,
)


router = APIRouter(prefix="/academic-years", tags=["Academic Years"])
require_school_admin = require_roles(AccountType.SCHOOL_ADMIN)


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
