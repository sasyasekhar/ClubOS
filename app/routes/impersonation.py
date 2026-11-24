from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.routes.auth import require_auth_context
from app.schemas.activity import PublishedActivity
from app.schemas.impersonation import (
    AccountSearch,
    EligibleAccountPage,
    EndImpersonationRequest,
    ImpersonationState,
    StartImpersonationRequest,
)
from app.services.impersonation import (
    ImpersonationError,
    current_impersonation,
    end_impersonation,
    list_eligible_accounts,
    require_actual_admin_school,
    require_publications_viewer,
    start_impersonation,
    view_published_activities,
)
from app.services.sessions import AuthenticationContext


router = APIRouter(prefix="/admin/impersonation", tags=["Administrator Impersonation"])
PRIVATE_HEADERS = {"Cache-Control": "private, no-store"}


def require_actual_administrator(context: AuthenticationContext = Depends(require_auth_context)) -> AuthenticationContext:
    try:
        require_actual_admin_school(context)
    except ImpersonationError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc
    return context


def require_impersonated_publications(context: AuthenticationContext = Depends(require_auth_context)) -> AuthenticationContext:
    try:
        require_publications_viewer(context)
    except ImpersonationError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc
    return context


@router.get("/eligible-accounts")
def eligible_impersonation_accounts(
    response: Response,
    limit: int = Query(default=20, ge=1, le=50),
    after: UUID | None = None,
    q: AccountSearch | None = None,
    context: AuthenticationContext = Depends(require_actual_administrator),
    db: Session = Depends(get_db),
) -> EligibleAccountPage:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return list_eligible_accounts(db, context, limit=limit, after=after, search=q)
    except ImpersonationError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.post("/start", status_code=status.HTTP_201_CREATED)
def begin_impersonation(
    data: StartImpersonationRequest,
    response: Response,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    context: AuthenticationContext = Depends(require_actual_administrator),
    db: Session = Depends(get_db),
) -> ImpersonationState:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return start_impersonation(db, context, data, idempotency_key)
    except ImpersonationError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.get("/current")
def get_current_impersonation(
    response: Response,
    context: AuthenticationContext = Depends(require_actual_administrator),
    db: Session = Depends(get_db),
) -> ImpersonationState:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return current_impersonation(db, context)
    except ImpersonationError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.post("/end")
def stop_impersonation(
    data: EndImpersonationRequest,
    response: Response,
    context: AuthenticationContext = Depends(require_actual_administrator),
    db: Session = Depends(get_db),
) -> ImpersonationState:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return end_impersonation(db, context, data)
    except ImpersonationError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.get("/published-activities")
def view_impersonated_publications(
    response: Response,
    limit: int = Query(default=20, ge=1, le=50),
    context: AuthenticationContext = Depends(require_impersonated_publications),
    db: Session = Depends(get_db),
) -> list[PublishedActivity]:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return view_published_activities(db, context, limit)
    except ImpersonationError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc
