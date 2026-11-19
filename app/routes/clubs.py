
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.models.account import Account
from app.models.enums import AccountType
from app.security.permissions import require_roles
from app.schemas.club import (
    ClubRegistrationRequest,
    ClubRegistrationResponse,
)
from app.services.club_registration import (
    register_club,
    ClubRequestError,
)
from app.schemas.club import ClubReviewRequest, ClubReviewResponse
from app.services.club_review import review_club

router = APIRouter(prefix="/clubs", tags=["Clubs"])


@router.post(
    "",
    response_model=ClubRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_club_request(
    payload: ClubRegistrationRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    student: Account = Depends(require_roles(AccountType.STUDENT)),
    db: Session = Depends(get_db),
):
    try:
        return register_club(
            db, student, payload, idempotency_key
        )
    except ClubRequestError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc
@router.post(
    "/{club_id}/reviews",
    response_model=ClubReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_club_review(
    club_id: UUID,
    payload: ClubReviewRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    admin: Account = Depends(
        require_roles(AccountType.SCHOOL_ADMIN)
    ),
    db: Session = Depends(get_db),
):
    try:
        return review_club(
            db, admin, club_id, payload, idempotency_key
        )
    except ClubRequestError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc
