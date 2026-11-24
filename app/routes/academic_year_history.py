from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.models.account import Account
from app.routes.auth import require_account
from app.schemas.academic_year_history import AcademicYearHistoryPage, HistoricalActivityPage
from app.services.activity_drafts import ActivityError
from app.services.activity_publications import list_historical_academic_years, list_historical_publications


router = APIRouter(prefix="/academic-years", tags=["Academic Years"])
PRIVATE_HEADERS = {"Cache-Control": "private, no-store"}


@router.get("/history")
def academic_year_history(
    response: Response,
    limit: int = Query(default=20, ge=1, le=50),
    after: str | None = Query(default=None, max_length=47),
    viewer: Account = Depends(require_account),
    db: Session = Depends(get_db),
) -> AcademicYearHistoryPage:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return list_historical_academic_years(db, viewer, limit, after)
    except ActivityError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.get("/{year_id}/published-activities")
def historical_activities(
    year_id: UUID,
    response: Response,
    limit: int = Query(default=20, ge=1, le=50),
    after: str | None = Query(default=None, max_length=47),
    viewer: Account = Depends(require_account),
    db: Session = Depends(get_db),
) -> HistoricalActivityPage:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return list_historical_publications(db, viewer, year_id, limit, after)
    except ActivityError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc
