from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.models.account import Account
from app.models.enums import AccountType
from app.routes.auth import PRIVATE_HEADERS
from app.schemas.dashboard import Dashboard, DashboardClub, DashboardQuery
from app.security.permissions import require_roles
from app.services.activity_drafts import ActivityError
from app.services.club_registration import ClubRequestError
from app.services.dashboard import get_dashboard, get_dashboard_club


router = APIRouter(prefix="/dashboard", tags=["Member Dashboard"])
require_member = require_roles(AccountType.STUDENT, AccountType.TEACHER)


@router.get("")
def view_dashboard(
    query: Annotated[DashboardQuery, Query()],
    response: Response,
    viewer: Account = Depends(require_member),
    db: Session = Depends(get_db),
) -> Dashboard:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return get_dashboard(db, viewer, query)
    except (ActivityError, ClubRequestError) as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.get("/clubs/{club_id}")
def view_dashboard_club(
    club_id: UUID,
    response: Response,
    viewer: Account = Depends(require_member),
    db: Session = Depends(get_db),
) -> DashboardClub:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return get_dashboard_club(db, viewer, club_id)
    except ActivityError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc
