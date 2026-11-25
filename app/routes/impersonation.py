from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.routes.activities import ATTACHMENTS_PATH, REVISION_PATH, attachment_download_response
from app.routes.auth import PRIVATE_HEADERS, require_auth_context, require_impersonation_write_context
from app.schemas.activity import ActivityReviewResponse, PublishedActivity
from app.schemas.dashboard import Dashboard, DashboardClub, DashboardQuery, DashboardRevisionDetail, RevisionDetailQuery
from app.schemas.impersonation import (
    AccountSearch,
    EligibleAccountPage,
    EndImpersonationRequest,
    ImpersonatedActivityReviewRequest,
    ImpersonationState,
    StartImpersonationRequest,
)
from app.services.activity_attachments import load_activity_attachment
from app.services.activity_review import review_impersonated_activity
from app.services.dashboard import authorized_dashboard_revision, get_dashboard, get_dashboard_club, get_dashboard_revision
from app.services.impersonation import (
    ImpersonationError,
    audited_impersonated_read,
    current_impersonation,
    end_impersonation,
    list_eligible_accounts,
    start_impersonation,
    view_published_activities,
)
from app.services.sessions import AuthenticationContext
from app.services.storage import AttachmentStorage, get_attachment_storage


router = APIRouter(prefix="/admin/impersonation", tags=["Administrator Impersonation"])


@router.get("/eligible-accounts")
def eligible_impersonation_accounts(
    response: Response,
    limit: int = Query(default=20, ge=1, le=50),
    after: UUID | None = None,
    q: AccountSearch | None = None,
    context: AuthenticationContext = Depends(require_auth_context),
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
    context: AuthenticationContext = Depends(require_impersonation_write_context),
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
    context: AuthenticationContext = Depends(require_auth_context),
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
    context: AuthenticationContext = Depends(require_auth_context),
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
    context: AuthenticationContext = Depends(require_auth_context),
    db: Session = Depends(get_db),
) -> list[PublishedActivity]:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return view_published_activities(db, context, limit)
    except ImpersonationError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.get("/dashboard")
def view_impersonated_dashboard(
    query: Annotated[DashboardQuery, Query()],
    response: Response,
    context: AuthenticationContext = Depends(require_auth_context),
    db: Session = Depends(get_db),
) -> Dashboard:
    response.headers.update(PRIVATE_HEADERS)
    try:
        with audited_impersonated_read(
            db, context, action="IMPERSONATION_DASHBOARD_VIEWED", entity_type="account",
            entity_id=context.effective_account.id,
        ) as viewer:
            return get_dashboard(db, viewer, query)
    except ImpersonationError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.get("/dashboard/clubs/{club_id}")
def view_impersonated_club(
    club_id: UUID,
    response: Response,
    context: AuthenticationContext = Depends(require_auth_context),
    db: Session = Depends(get_db),
) -> DashboardClub:
    response.headers.update(PRIVATE_HEADERS)
    try:
        with audited_impersonated_read(
            db, context, action="IMPERSONATION_CLUB_VIEWED", entity_type="club", entity_id=club_id,
        ) as viewer:
            return get_dashboard_club(db, viewer, club_id)
    except ImpersonationError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.get("/clubs" + REVISION_PATH)
def view_impersonated_revision(
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    query: Annotated[RevisionDetailQuery, Query()],
    response: Response,
    context: AuthenticationContext = Depends(require_auth_context),
    db: Session = Depends(get_db),
) -> DashboardRevisionDetail:
    response.headers.update(PRIVATE_HEADERS)
    try:
        with audited_impersonated_read(
            db, context, action="IMPERSONATION_REVISION_VIEWED", entity_type="activity_revision", entity_id=revision_id,
        ) as viewer:
            return get_dashboard_revision(db, viewer, club_id, activity_id, revision_id, query)
    except ImpersonationError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.get("/clubs" + ATTACHMENTS_PATH + "/{attachment_id}", response_class=Response)
def download_impersonated_attachment(
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    attachment_id: UUID,
    context: AuthenticationContext = Depends(require_auth_context),
    db: Session = Depends(get_db),
    storage: AttachmentStorage = Depends(get_attachment_storage),
) -> Response:
    try:
        with audited_impersonated_read(
            db, context, action="IMPERSONATION_ATTACHMENT_DOWNLOADED", entity_type="activity_attachment",
            entity_id=attachment_id,
        ) as viewer:
            authorized_dashboard_revision(db, viewer, club_id, activity_id, revision_id)
            attachment = load_activity_attachment(db, storage, viewer, club_id, activity_id, revision_id, attachment_id)
            return attachment_download_response(attachment)
    except ImpersonationError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.post("/clubs" + REVISION_PATH + "/review", status_code=status.HTTP_201_CREATED)
def review_as_impersonated_teacher(
    club_id: UUID,
    activity_id: UUID,
    revision_id: UUID,
    data: ImpersonatedActivityReviewRequest,
    response: Response,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    context: AuthenticationContext = Depends(require_impersonation_write_context),
    db: Session = Depends(get_db),
) -> ActivityReviewResponse:
    response.headers.update(PRIVATE_HEADERS)
    try:
        return review_impersonated_activity(
            db, context, data.impersonation_session_id, club_id, activity_id, revision_id, data, idempotency_key,
        )
    except ImpersonationError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc
