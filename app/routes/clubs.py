
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
