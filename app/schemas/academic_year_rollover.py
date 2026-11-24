from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import ClubStatus


MAX_ROLLOVER_CLUBS = 50


class ClubRolloverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_club_ids: list[UUID] = Field(min_length=1, max_length=MAX_ROLLOVER_CLUBS)

    @field_validator("source_club_ids")
    @classmethod
    def distinct_sources(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Source club IDs must be distinct.")
        return value


class ClubRolloverItem(BaseModel):
    source_club_id: UUID
    club_id: UUID


class ClubRolloverResponse(BaseModel):
    target_year_id: UUID
    items: list[ClubRolloverItem]


class RolloverCandidate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_club_id: UUID
    name: str
    description: str | None
    category: str
    source_academic_year_id: UUID
    source_academic_year_name: str
    previous_status: ClubStatus
    already_carried_forward: bool


class RolloverCandidatesPage(BaseModel):
    items: list[RolloverCandidate]
    next_cursor: UUID | None
