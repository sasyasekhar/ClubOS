
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.config.database import get_db
from app.models.account import Account
from app.models.enums import AccountType
from app.security.permissions import require_roles
from app.schemas.club import (
    ClubRegistrationRequest,
    ClubRegistrationResponse,
)
from app.services.club_registration import (
    register_club,
    ClubRequestError,
)
from app.schemas.club import ClubReviewRequest, ClubReviewResponse
from app.services.club_review import review_club

from app.services.club_resubmission import resubmit_club


from app.schemas.teacher_invitation import (
    EligibleTeachersPage,
    TeacherInvitationDecision,
    TeacherInvitationResponse,
    PendingTeacherInvitation,
)
from app.services.teacher_invitation import (
    list_teacher_invitations,
    respond_to_invitation,
)

from pydantic import BaseModel
from app.models.enums import MembershipStatus
from app.services.teacher_replacement import list_eligible_teachers, replace_declined_teacher, select_rollover_teacher


from app.schemas.membership import (
    EndMembershipRequest,
    MembershipResponse,
)
from app.services.representative_memberships import (
    end_representative,
    rejoin_representative,
)


router = APIRouter(prefix="/clubs", tags=["Clubs"])
PRIVATE_HEADERS = {"Cache-Control": "private, no-store"}


@router.post(
    "",
    response_model=ClubRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_club_request(
    payload: ClubRegistrationRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    student: Account = Depends(require_roles(AccountType.STUDENT)),
    db: Session = Depends(get_db),
):
    try:
        return register_club(
            db, student, payload, idempotency_key
        )
    except ClubRequestError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc
@router.post(
    "/{club_id}/reviews",
    response_model=ClubReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_club_review(
    club_id: UUID,
    payload: ClubReviewRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    admin: Account = Depends(
        require_roles(AccountType.SCHOOL_ADMIN)
    ),
    db: Session = Depends(get_db),
):
    try:
        return review_club(
            db, admin, club_id, payload, idempotency_key
        )
    except ClubRequestError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc


@router.post(
    "/{club_id}/resubmit",
    response_model=ClubRegistrationResponse,
    status_code=status.HTTP_200_OK,
)
def resubmit_club_request(
    club_id: UUID,
    payload: ClubRegistrationRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    student: Account = Depends(
        require_roles(AccountType.STUDENT)
    ),
    db: Session = Depends(get_db),
):
    try:
        return resubmit_club(
            db, student, club_id, payload, idempotency_key
        )
    except ClubRequestError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

@router.get(
    "/teacher-invitations",
    response_model=list[PendingTeacherInvitation],
)
def get_teacher_invitations(
    teacher: Account = Depends(require_roles(AccountType.TEACHER)),
    db: Session = Depends(get_db),
):
    return list_teacher_invitations(db, teacher)


@router.post(
    "/{club_id}/teacher-invitation",
    response_model=TeacherInvitationResponse,
)
def reply_to_teacher_invitation(
    club_id: UUID,
    payload: TeacherInvitationDecision,
    teacher: Account = Depends(require_roles(AccountType.TEACHER)),
    db: Session = Depends(get_db),
):
    try:
        return respond_to_invitation(
            db, teacher, club_id, payload.decision, payload.membership_id
        )
    except ClubRequestError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc

class TeacherReplacementRequest(BaseModel):
    teacher_account_id: UUID


class TeacherReplacementResponse(BaseModel):
    club_id: UUID
    membership_id: UUID
    teacher_account_id: UUID
    status: MembershipStatus


@router.get("/{club_id}/eligible-teachers", response_model=EligibleTeachersPage)
def browse_eligible_teachers(
    club_id: UUID,
    response: Response,
    limit: int = Query(default=20, ge=1, le=50),
    after: UUID | None = None,
    student: Account = Depends(require_roles(AccountType.STUDENT)),
    db: Session = Depends(get_db),
):
    response.headers.update(PRIVATE_HEADERS)
    try:
        return list_eligible_teachers(db, student, club_id, limit=limit, after=after)
    except ClubRequestError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.post(
    "/{club_id}/select-teacher",
    response_model=TeacherReplacementResponse,
    status_code=status.HTTP_201_CREATED,
)
def select_teacher_for_rollover(
    club_id: UUID,
    payload: TeacherReplacementRequest,
    response: Response,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    student: Account = Depends(require_roles(AccountType.STUDENT)),
    db: Session = Depends(get_db),
):
    response.headers.update(PRIVATE_HEADERS)
    try:
        return select_rollover_teacher(db, student, club_id, payload.teacher_account_id, idempotency_key)
    except ClubRequestError as exc:
        raise HTTPException(exc.status_code, exc.detail, headers=PRIVATE_HEADERS) from exc


@router.post(
    "/{club_id}/replace-teacher",
    response_model=TeacherReplacementResponse,
    status_code=201,
)
def replace_teacher(
    club_id: UUID,
    payload: TeacherReplacementRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    student: Account = Depends(require_roles(AccountType.STUDENT)),
    db: Session = Depends(get_db),
):
    try:
        return replace_declined_teacher(
            db,
            student,
            club_id,
            payload.teacher_account_id,
            idempotency_key,
        )
    except ClubRequestError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc


@router.post(
    "/{club_id}/memberships/{membership_id}/end",
    response_model=MembershipResponse,
)
def end_club_representative(
    club_id: UUID,
    membership_id: UUID,
    payload: EndMembershipRequest,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    admin: Account = Depends(
        require_roles(AccountType.SCHOOL_ADMIN)
    ),
    db: Session = Depends(get_db),
):
    try:
        return end_representative(
            db,
            admin,
            club_id,
            membership_id,
            idempotency_key,
            payload.reason,
        )
    except ClubRequestError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc


@router.post(
    "/{club_id}/memberships/{membership_id}/rejoin",
    response_model=MembershipResponse,
    status_code=201,
)
def rejoin_club_representative(
    club_id: UUID,
    membership_id: UUID,
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
    admin: Account = Depends(
        require_roles(AccountType.SCHOOL_ADMIN)
    ),
    db: Session = Depends(get_db),
):
    try:
        return rejoin_representative(
            db,
            admin,
            club_id,
            membership_id,
            idempotency_key,
        )
    except ClubRequestError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc
