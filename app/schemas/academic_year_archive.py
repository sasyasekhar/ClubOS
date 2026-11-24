from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool

from app.schemas.academic_year import AcademicYearResponse, UtcTimestamp


class AcademicYearArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    export_saved: StrictBool


class ArchivedAcademicYearResponse(AcademicYearResponse):
    archived_at: UtcTimestamp
    archived_by_account_id: UUID
    final_export_id: UUID
    final_export_generated_at: UtcTimestamp
