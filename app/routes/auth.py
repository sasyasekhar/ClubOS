
from fastapi import APIRouter, Depends, HTTPException, status
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

from fastapi import Request, Response, Header

from app.config.settings import settings
from app.models.school import School
from app.schemas.auth import LoginRequest, LoginResponse, AccountResponse
from app.services.authentication import (
    authenticate_account,
    InvalidCredentials,
)
from app.services.sessions import (
    create_session,
    current_account,
    revoke_session,
    record_failed_login,
    InvalidSession,
)


router = APIRouter()


@router.post(
    "/register-school",
    response_model=SchoolRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
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

COOKIE_NAME = "clubos_session"


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
    except InvalidSession:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
        )


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    try:
        account = authenticate_account(
            db, payload.identifier, payload.password
        )

        if account.school_id is not None:
            school = db.get(School, account.school_id)
            if school is None or not school.is_active:
                raise InvalidCredentials()

    except InvalidCredentials:
        record_failed_login(db, payload.identifier)
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
        )

    cookie_token, tab_key = create_session(
        db, account, payload.identifier
    )

    response.set_cookie(
        key=COOKIE_NAME,
        value=cookie_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
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
def heartbeat(account=Depends(require_account)):
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

    revoke_session(
        db,
        request.cookies.get(COOKIE_NAME),
        tab_key,
    )

    response.delete_cookie(COOKIE_NAME, path="/", samesite="lax")
