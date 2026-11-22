import logging
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.academic_year import AcademicYear
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.enums import AcademicYearStatus, AccountType
from app.models.school import School
from app.models.school_admin import SchoolAdmin
from app.schemas.academic_year import AcademicYearCreate, AcademicYearResponse


logger = logging.getLogger(__name__)

CONSTRAINT_CONFLICTS = {
    "uq_academic_year_school_name": "An academic year with this name already exists.",
    "uq_one_active_academic_year_per_school": "The school already has an active academic year.",
}


class AcademicYearError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _require_admin_school(db: Session, admin: Account, *, for_update: bool = False) -> UUID:
    if (
        admin.account_type != AccountType.SCHOOL_ADMIN
        or not admin.is_active
        or admin.school_id is None
    ):
        raise AcademicYearError(403, "Permission denied.")

    query = (
        select(School.id)
        .join(SchoolAdmin, SchoolAdmin.school_id == School.id)
        .where(
            School.id == admin.school_id,
            School.is_active.is_(True),
            SchoolAdmin.account_id == admin.id,
        )
    )
    if for_update:
        # A school can have no academic years yet. Lock its parent row before
        # checking for a replay or trying to insert the first year.
        query = query.with_for_update(of=(School, SchoolAdmin))

    school_id = db.scalar(query)
    if school_id is None:
        raise AcademicYearError(403, "Permission denied.")
    return school_id


def list_academic_years(db: Session, admin: Account) -> list[AcademicYearResponse]:
    school_id = _require_admin_school(db, admin)
    years = db.scalars(
        select(AcademicYear)
        .where(AcademicYear.school_id == school_id)
        .order_by(AcademicYear.start_date.desc(), AcademicYear.id)
    )
    return [AcademicYearResponse.model_validate(year) for year in years]


def create_academic_year(
    db: Session,
    admin: Account,
    data: AcademicYearCreate,
    idempotency_key: UUID,
) -> AcademicYearResponse:
    try:
        school_id = _require_admin_school(db, admin, for_update=True)
        year_id = uuid5(NAMESPACE_URL, f"clubos:academic-year:{school_id}:{idempotency_key}")
        existing = db.scalar(
            select(AcademicYear).where(
                AcademicYear.id == year_id, AcademicYear.school_id == school_id
            )
        )
        if existing is not None:
            if (
                existing.name != data.name
                or existing.start_date != data.start_date
                or existing.end_date != data.end_date
            ):
                raise AcademicYearError(409, "Idempotency key reused with different data.")

            response = AcademicYearResponse.model_validate(existing)
            db.rollback()
            return response

        year = AcademicYear(
            id=year_id,
            school_id=school_id,
            name=data.name,
            start_date=data.start_date,
            end_date=data.end_date,
            status=AcademicYearStatus.ACTIVE,
        )
        db.add_all([
            year,
            AuditLog(
                actor_account_id=admin.id,
                action="ACADEMIC_YEAR_CREATED",
                entity_type="academic_year",
                entity_id=str(year_id),
                details={"school_id": str(school_id), "status": year.status.value},
            ),
        ])
        db.flush()
        response = AcademicYearResponse.model_validate(year)
        db.commit()
        return response
    except AcademicYearError:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        if isinstance(exc, IntegrityError):
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if constraint in CONSTRAINT_CONFLICTS:
                raise AcademicYearError(409, CONSTRAINT_CONFLICTS[constraint]) from exc
        logger.error("Academic year creation failed (%s)", type(exc).__name__)
        raise AcademicYearError(503, "Academic year creation is temporarily unavailable.") from exc
