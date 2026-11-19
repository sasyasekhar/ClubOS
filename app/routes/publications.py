
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.models.account import Account
from app.routes.auth import require_account
from app.schemas.activity import PublishedActivity
from app.services.activity_drafts import ActivityError
from app.services.activity_publications import (
    list_published_activities,
)


router = APIRouter(
    prefix="/activities",
    tags=["Activity Publications"],
)


@router.get(
    "/published",
    response_model=list[PublishedActivity],
)
def published_activities(
    limit: int = Query(default=20, ge=1, le=50),
    viewer: Account = Depends(require_account),
    db: Session = Depends(get_db),
):
    try:
        return list_published_activities(db, viewer, limit)
    except ActivityError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc
