from pydantic import BaseModel

from app.schemas.academic_year import AcademicYearResponse
from app.schemas.activity import PublishedActivity


class AcademicYearHistoryPage(BaseModel):
    items: list[AcademicYearResponse]
    next_cursor: str | None


class HistoricalActivityPage(BaseModel):
    items: list[PublishedActivity]
    next_cursor: str | None
