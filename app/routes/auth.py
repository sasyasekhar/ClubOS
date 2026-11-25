
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.schemas.auth import (
    SchoolRegistration,
    SchoolRegistrationResponse,
)
from app.services.school_registration import (
    register_school,
    RegistrationConflict,
)
from app.schemas.auth import (
    MemberRegistration,
    MemberRegistrationResponse,
)

from app.services.member_registration import (
    register_member,
    SchoolNotAvailable,
)

from app.config.settings import settings
from app.models.school import School
from app.schemas.auth import LoginRequest, LoginResponse, AccountResponse
from app.services.authentication import (
    authenticate_account,
    InvalidCredentials,
)
from app.services.login_throttling import LoginThrottled, WINDOW_SECONDS, lock_login_attempt
from app.services.sessions import (
    AuthenticationContext,
    ImpersonationRestricted,
    InvalidSession,
    authentication_context,
    create_session,
    current_account,
    record_failed_login,
    revoke_session,
)


router = APIRouter()
logger = logging.getLogger(__name__)
COOKIE_NAME = "clubos_session"
PRIVATE_HEADERS = {"Cache-Control": "private, no-store"}


def _impersonation_restricted(detail: str = "End impersonation before using this operation.") -> HTTPException:
    return HTTPException(403, detail, headers=PRIVATE_HEADERS)


def _authentication_unavailable(db: Session, exc: SQLAlchemyError) -> HTTPException:
    db.rollback()
    logger.error("Authentication database operation failed (%s)", type(exc).__name__)
    return HTTPException(503, "Authentication is temporarily unavailable.", headers=PRIVATE_HEADERS)


def reject_impersonation_on_public_route(
    request: Request,
    db: Session = Depends(get_db),
    tab_key: str | None = Header(default=None, alias="X-ClubOS-Tab"),
):
    cookie_token = request.cookies.get(COOKIE_NAME)
    if not cookie_token or not tab_key:
        return
    try:
        context = authentication_context(db, cookie_token, tab_key, renew=False)
        if context.is_impersonating or context.impersonation_invalidated:
            raise _impersonation_restricted()
    except InvalidSession:
        # These operations also serve unauthenticated clients.
        return
    except SQLAlchemyError as exc:
        raise _authentication_unavailable(db, exc) from exc


@router.post(
    "/register-school",
    response_model=SchoolRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(reject_impersonation_on_public_route)],
)
def create_school(
    payload: SchoolRegistration,
    db: Session = Depends(get_db),
):
    try:
        return register_school(db, payload)
    except RegistrationConflict:
        raise HTTPException(
            status_code=409,
            detail="Registration could not be completed.",
        )
    
@router.post(
    "/register",
    response_model=MemberRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(reject_impersonation_on_public_route)],
)
def create_member(
    payload: MemberRegistration,
    db: Session = Depends(get_db),
):
    try:
        return register_member(db, payload)

    except SchoolNotAvailable:
        raise HTTPException(
            status_code=400,
            detail="Invalid or inactive School Code.",
        )

    except RegistrationConflict:
        raise HTTPException(
            status_code=409,
            detail="Registration could not be completed.",
        )

def require_account(
    request: Request,
    db: Session = Depends(get_db),
    tab_key: str | None = Header(default=None, alias="X-ClubOS-Tab"),
):
    try:
        return current_account(
            db,
            request.cookies.get(COOKIE_NAME),
            tab_key,
        )
    except InvalidSession as exc:
        raise HTTPException(401, "Authentication required", headers=PRIVATE_HEADERS) from exc
    except ImpersonationRestricted as exc:
        raise _impersonation_restricted(str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _authentication_unavailable(db, exc) from exc


def require_auth_context(
    request: Request,
    db: Session = Depends(get_db),
    tab_key: str | None = Header(default=None, alias="X-ClubOS-Tab"),
) -> AuthenticationContext:
    """Resolve context for the explicitly allowed impersonation routes."""
    return _resolve_auth_context(request, db, tab_key, renew=True)


def require_impersonation_write_context(
    request: Request,
    db: Session = Depends(get_db),
    tab_key: str | None = Header(default=None, alias="X-ClubOS-Tab"),
) -> AuthenticationContext:
    return _resolve_auth_context(request, db, tab_key, renew=False)


def _resolve_auth_context(request: Request, db: Session, tab_key: str | None, *, renew: bool) -> AuthenticationContext:
    try:
        return authentication_context(db, request.cookies.get(COOKIE_NAME), tab_key, lock_admin=True, renew=renew)
    except InvalidSession as exc:
        raise HTTPException(401, "Authentication required", headers=PRIVATE_HEADERS) from exc
    except SQLAlchemyError as exc:
        raise _authentication_unavailable(db, exc) from exc


@router.post("/login", response_model=LoginResponse, dependencies=[Depends(reject_impersonation_on_public_route)])
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    try:
        client, identifier = lock_login_attempt(db, payload.identifier, request.client.host if request.client else "unknown")
        account = authenticate_account(
            db, payload.identifier, payload.password
        )

        if account.school_id is not None:
            school = db.get(School, account.school_id)
            if school is None or not school.is_active:
                raise InvalidCredentials()

    except LoginThrottled as exc:
        raise HTTPException(
            429, "Too many login attempts. Try again later.",
            headers={**PRIVATE_HEADERS, "Retry-After": str(WINDOW_SECONDS)},
        ) from exc
    except InvalidCredentials:
        record_failed_login(db, client, identifier)
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers=PRIVATE_HEADERS,
        )

    cookie_token, tab_key = create_session(
        db, account, payload.identifier
    )

    response.set_cookie(
        key=COOKIE_NAME,
        value=cookie_token,
        httponly=True,
        secure=settings.COOKIE_SECURE or settings.APP_ENV == "production",
        samesite="lax",
        path="/",
    )

    response.headers["Cache-Control"] = "no-store"

    return {"account": account, "tab_key": tab_key}


@router.get("/me", response_model=AccountResponse)
def get_me(
    response: Response,
    account=Depends(require_account),
):
    response.headers["Cache-Control"] = "no-store"
    return account


@router.post("/heartbeat")
def heartbeat(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    tab_key: str | None = Header(default=None, alias="X-ClubOS-Tab"),
):
    try:
        # Session maintenance does not grant either account's business access.
        authentication_context(db, request.cookies.get(COOKIE_NAME), tab_key)
    except InvalidSession as exc:
        raise HTTPException(401, "Authentication required", headers=PRIVATE_HEADERS) from exc
    except SQLAlchemyError as exc:
        raise _authentication_unavailable(db, exc) from exc
    response.headers.update(PRIVATE_HEADERS)
    return {"status": "active"}


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    tab_key: str | None = Header(default=None, alias="X-ClubOS-Tab"),
):
    if not tab_key:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        revoke_session(db, request.cookies.get(COOKIE_NAME), tab_key)
    except SQLAlchemyError as exc:
        raise _authentication_unavailable(db, exc) from exc

    response.headers.update(PRIVATE_HEADERS)
    response.delete_cookie(COOKIE_NAME, path="/", samesite="lax", httponly=True,
                           secure=settings.COOKIE_SECURE or settings.APP_ENV == "production")
