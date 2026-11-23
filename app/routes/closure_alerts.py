from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.models.account import Account
from app.models.enums import AccountType
from app.schemas.closure_alerts import PendingReviewAlertPage
from app.security.permissions import require_roles
from app.services.academic_years import AcademicYearError
from app.services.closure_alerts import list_pending_review_alerts


router = APIRouter(prefix="/academic-years", tags=["Academic Years"])
require_alert_viewer = require_roles(AccountType.TEACHER, AccountType.SCHOOL_ADMIN)
PRIVATE_HEADERS = {"Cache-Control": "private, no-store"}


@router.get("/{year_id}/pending-review-alerts")
def pending_review_alerts(
    year_id: UUID,
    response: Response,
    limit: int = Query(default=20, ge=1, le=50),
    after: UUID | None = None,
    attention_only: bool = False,
    viewer: Account = Depends(require_alert_viewer),
    db: Session = Depends(get_db),
) -> PendingReviewAlertPage:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return list_pending_review_alerts(
            db, viewer, year_id, limit=limit, after=after, attention_only=attention_only,
        )
    except AcademicYearError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc
