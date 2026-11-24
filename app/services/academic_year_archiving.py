import logging
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.academic_year import AcademicYear
from app.models.academic_year_final_export import AcademicYearFinalExport
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.enums import AcademicYearStatus
from app.schemas.academic_year import AcademicYearResponse
from app.schemas.academic_year_archive import ArchivedAcademicYearResponse
from app.services.academic_year_write_guard import lock_academic_year_for_write
from app.services.academic_years import AcademicYearError, require_admin_school


logger = logging.getLogger(__name__)
ARCHIVE_ACTION = "ACADEMIC_YEAR_ARCHIVED"


def _archive_response(year: AcademicYear, receipt: AcademicYearFinalExport) -> ArchivedAcademicYearResponse:
    return ArchivedAcademicYearResponse(
        **AcademicYearResponse.model_validate(year).model_dump(),
        archived_at=receipt.archived_at,
        archived_by_account_id=receipt.archived_by_account_id,
        final_export_id=receipt.export_id,
        final_export_generated_at=receipt.generated_at,
    )


def archive_academic_year(
    db: Session, admin: Account, year_id: UUID, idempotency_key: UUID, *, export_saved: bool,
) -> ArchivedAcademicYearResponse:
    try:
        db.execute(text("SET LOCAL statement_timeout = '10s'"))
        school_id = require_admin_school(db, admin, for_update=True)
        if export_saved is not True:
            raise AcademicYearError(422, "Confirm that the final export was saved before archiving.")

        year = lock_academic_year_for_write(db, school_id, year_id, exclusive=True)
        if year is None:
            raise AcademicYearError(404, "Academic year not found.")

        replay = db.scalar(select(AcademicYearFinalExport).where(
            AcademicYearFinalExport.school_id == school_id,
            AcademicYearFinalExport.archive_idempotency_key == idempotency_key,
        ).execution_options(populate_existing=True))
        if replay is not None:
            if replay.academic_year_id != year.id:
                raise AcademicYearError(409, "Idempotency key reused for another academic year.")
            response = _archive_response(year, replay)
            db.rollback()
            return response

        if year.status != AcademicYearStatus.CLOSED:
            raise AcademicYearError(409, "Only a closed academic year can be archived.")

        receipt = db.scalar(select(AcademicYearFinalExport).where(
            AcademicYearFinalExport.school_id == school_id,
            AcademicYearFinalExport.academic_year_id == year.id,
        ).execution_options(populate_existing=True))
        if receipt is None:
            raise AcademicYearError(409, {
                "code": "ACADEMIC_YEAR_FINAL_EXPORT_REQUIRED",
                "message": "Generate a final export after closure before archiving this year.",
            })

        receipt.archived_at = db.execute(select(func.clock_timestamp())).scalar_one()
        receipt.archived_by_account_id = admin.id
        receipt.archive_idempotency_key = idempotency_key
        # The database transition guard checks the recorded export and acknowledgement.
        db.flush()
        year.status = AcademicYearStatus.ARCHIVED
        db.add(AuditLog(
            actor_account_id=admin.id,
            action=ARCHIVE_ACTION,
            entity_type="academic_year",
            entity_id=str(year.id),
            details={"school_id": str(school_id), "final_export_id": str(receipt.export_id), "export_saved": True},
            occurred_at=receipt.archived_at,
        ))
        response = _archive_response(year, receipt)
        db.commit()
        return response
    except AcademicYearError:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Academic-year archiving failed (%s)", type(exc).__name__)
        raise AcademicYearError(503, "Academic-year archiving is temporarily unavailable.") from exc
