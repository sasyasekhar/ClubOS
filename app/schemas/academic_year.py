import re
from datetime import date, datetime
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator, model_validator

from app.models.enums import AcademicYearStatus


class AcademicYearCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    start_date: date
    end_date: date

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Name cannot contain a null character.")
        return value

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def validate_date_format(cls, value: object) -> object:
        if type(value) is date or (
            isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
        ):
            return value
        raise ValueError("Use a calendar date in YYYY-MM-DD format.")

    @model_validator(mode="after")
    def validate_date_order(self) -> Self:
        if self.end_date <= self.start_date:
            raise ValueError("End date must be after start date.")
        return self


class AcademicYearResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    start_date: date
    end_date: date
    status: AcademicYearStatus
    created_at: datetime
