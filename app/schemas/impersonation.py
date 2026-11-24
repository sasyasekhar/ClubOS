from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

from app.models.enums import AccountType, ImpersonationStatus


def _validate_text(value: str) -> str:
    if "\x00" in value or any("\ud800" <= character <= "\udfff" for character in value):
        raise ValueError("Text contains an invalid character.")
    return value


Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500), AfterValidator(_validate_text)]
AccountSearch = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100), AfterValidator(_validate_text)]
TargetRole = Literal[AccountType.STUDENT, AccountType.TEACHER]


class EligibleAccount(BaseModel):
    account_id: UUID
    full_name: str
    role: TargetRole


class EligibleAccountPage(BaseModel):
    items: list[EligibleAccount]
    next_cursor: UUID | None


class StartImpersonationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_account_id: UUID
    reason: Reason


class EndImpersonationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    impersonation_session_id: UUID
    reason: Reason | None = None


class ImpersonationState(BaseModel):
    active: bool = False
    impersonation_session_id: UUID | None = None
    represented_account_id: UUID | None = None
    represented_name: str | None = None
    represented_role: TargetRole | None = None
    reason: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: ImpersonationStatus | None = None
