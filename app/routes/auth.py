
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
