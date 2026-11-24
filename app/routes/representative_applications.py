from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.models.account import Account
from app.models.enums import AccountType
from app.schemas.representative_application import (
    MAX_REPRESENTATIVE_PAGE_SIZE,
    RepresentativeApplicantResponse,
    RepresentativeApplicationResponse,
    RepresentativeApplicationReviewRequest,
    RepresentativePage,
    RepresentativePosition,
    ReviewedRepresentativeApplicationResponse,
)
from app.security.permissions import require_roles
from app.services.club_registration import ClubRequestError
from app.services.representative_applications import (
    apply_for_representative_position,
    list_club_representative_applications,
    list_representative_positions,
    list_student_representative_applications,
    review_representative_application,
)


router = APIRouter(prefix="/clubs", tags=["Clubs"])
require_student = require_roles(AccountType.STUDENT)
require_school_admin = require_roles(AccountType.SCHOOL_ADMIN)
PRIVATE_HEADERS = {"Cache-Control": "private, no-store"}


@router.get("/available-representative-positions")
def available_representative_positions(
    response: Response,
    limit: int = Query(default=20, ge=1, le=MAX_REPRESENTATIVE_PAGE_SIZE),
    after: UUID | None = None,
    student: Account = Depends(require_student),
    db: Session = Depends(get_db),
) -> RepresentativePage[RepresentativePosition]:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return list_representative_positions(db, student, limit=limit, after=after)
    except ClubRequestError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.get("/my-representative-applications")
def my_representative_applications(
    response: Response,
    limit: int = Query(default=20, ge=1, le=MAX_REPRESENTATIVE_PAGE_SIZE),
    after: UUID | None = None,
    student: Account = Depends(require_student),
    db: Session = Depends(get_db),
) -> RepresentativePage[RepresentativeApplicationResponse]:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return list_student_representative_applications(db, student, limit=limit, after=after)
    except ClubRequestError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.post("/{club_id}/representative-applications", status_code=status.HTTP_201_CREATED)
def create_representative_application(
    club_id: UUID,
    response: Response,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    student: Account = Depends(require_student),
    db: Session = Depends(get_db),
) -> RepresentativeApplicationResponse:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return apply_for_representative_position(db, student, club_id, idempotency_key)
    except ClubRequestError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.get("/{club_id}/representative-applications")
def list_representative_applications(
    club_id: UUID,
    response: Response,
    limit: int = Query(default=20, ge=1, le=MAX_REPRESENTATIVE_PAGE_SIZE),
    after: UUID | None = None,
    admin: Account = Depends(require_school_admin),
    db: Session = Depends(get_db),
) -> RepresentativePage[RepresentativeApplicantResponse]:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return list_club_representative_applications(db, admin, club_id, limit=limit, after=after)
    except ClubRequestError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.post("/{club_id}/representative-applications/{application_id}/review")
def decide_representative_application(
    club_id: UUID,
    application_id: UUID,
    payload: RepresentativeApplicationReviewRequest,
    response: Response,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    admin: Account = Depends(require_school_admin),
    db: Session = Depends(get_db),
) -> ReviewedRepresentativeApplicationResponse:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return review_representative_application(db, admin, club_id, application_id, payload, idempotency_key)
    except ClubRequestError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc
