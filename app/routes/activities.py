
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.models.account import Account
from app.models.enums import AccountType
from app.security.permissions import require_roles

from app.schemas.activity import (
    ActivityDraftCreate,
    ActivityDraftResponse,
)
from app.services.activity_drafts import (
    ActivityError,
    create_activity_draft,
)


router = APIRouter(
    prefix="/clubs",
    tags=["Activities"],
)


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
