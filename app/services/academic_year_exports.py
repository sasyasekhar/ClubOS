import csv
import json
import logging
import re
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from io import StringIO
from itertools import chain
from tempfile import TemporaryFile
from time import monotonic
from typing import BinaryIO
from unicodedata import category, normalize
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, BadZipFile, LargeZipFile, ZipFile
from zlib import error as CompressionError

from sqlalchemy import Select, and_, func, literal, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from app.models.academic_year import AcademicYear
from app.models.academic_year_closure_request import AcademicYearClosureRequest
from app.models.academic_year_export_limit import AcademicYearExportLimit
from app.models.academic_year_final_export import AcademicYearFinalExport
from app.models.account import Account
from app.models.activity import Activity
from app.models.activity_attachment import ActivityAttachment
from app.models.activity_deadline_rejection import ActivityDeadlineRejection
from app.models.activity_review import ActivityReview
from app.models.activity_revision import ActivityRevision
from app.models.audit_log import AuditLog
from app.models.club import Club
from app.models.club_membership import ClubMembership
from app.models.club_review import ClubReview
from app.models.enums import AcademicYearStatus, ActivityRevisionStatus
from app.models.school_admin import SchoolAdmin
from app.services.academic_years import AcademicYearError, require_admin_school


logger = logging.getLogger(__name__)

EXPORT_ACTION = "ACADEMIC_YEAR_EXPORTED"
EXPORT_COOLDOWN_SECONDS = 60
MAX_EXPORT_ROWS = 50_000
MAX_EXPORT_BYTES = 32 * 1024 * 1024
MAX_EXPORT_SECONDS = 30
FETCH_ROWS = 500

UNSAFE_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _year_query(school_id: UUID, year_id: UUID) -> Select:
    return select(
        AcademicYear.id.label("academic_year_id"),
        AcademicYear.name,
        AcademicYear.start_date,
        AcademicYear.end_date,
        AcademicYear.status,
    ).where(AcademicYear.id == year_id, AcademicYear.school_id == school_id)


def _export_queries(db: Session, school_id: UUID, year_id: UUID) -> Iterator[tuple[str, str, Select]]:
    clubs = (
        select(Club.id.label("club_id"), Club.academic_year_id, Club.name, Club.category, Club.status)
        .join(AcademicYear, and_(
            AcademicYear.id == Club.academic_year_id,
            AcademicYear.school_id == Club.school_id,
        ))
        .where(AcademicYear.id == year_id, AcademicYear.school_id == school_id)
        .cte("export_clubs")
    )
    activities = (
        select(Activity.id.label("activity_id"), Activity.club_id, Activity.created_at)
        .join(clubs, clubs.c.club_id == Activity.club_id)
        .join(Account, Account.id == Activity.created_by)
        .where(Account.school_id == school_id)
        .cte("export_activities")
    )
    revisions = (
        select(
            ActivityRevision.id.label("revision_id"),
            ActivityRevision.activity_id,
            ActivityRevision.revision_number,
            ActivityRevision.title,
            ActivityRevision.activity_date,
            ActivityRevision.category,
            ActivityRevision.status,
            ActivityRevision.is_published,
            ActivityRevision.created_at,
            ActivityRevision.submitted_at,
            ActivityRevision.superseded_at,
            ActivityRevision.content_purged_at,
        )
        .join(activities, activities.c.activity_id == ActivityRevision.activity_id)
        .cte("export_revisions")
    )

    yield "academic_year.csv", "Name, dates, and status of the selected year.", _year_query(school_id, year_id)
    yield "clubs.csv", "Clubs and their status in the selected year.", select(clubs).order_by(clubs.c.club_id)
    source = aliased(Club)
    rollover_lineage = (
        select(
            clubs.c.club_id,
            source.id.label("source_club_id"),
            source.academic_year_id.label("source_academic_year_id"),
        )
        .select_from(clubs)
        .join(Club, Club.id == clubs.c.club_id)
        .join(source, and_(source.school_id == Club.school_id, source.id == Club.rolled_over_from_club_id))
    )
    if db.scalar(select(rollover_lineage.exists())):
        yield (
            "club_rollover_lineage.csv",
            "Historical source of each carried-forward club in the selected year.",
            rollover_lineage.order_by(clubs.c.club_id),
        )
    yield (
        "representative_memberships.csv",
        "Representative assignments and their date and status history.",
        select(
            ClubMembership.id.label("membership_id"),
            ClubMembership.club_id,
            ClubMembership.account_id.label("representative_account_id"),
            ClubMembership.role,
            ClubMembership.status,
            ClubMembership.start_date,
            ClubMembership.end_date,
        )
        .join(clubs, clubs.c.club_id == ClubMembership.club_id)
        .join(Account, Account.id == ClubMembership.account_id)
        .where(Account.school_id == school_id)
        .order_by(ClubMembership.id),
    )
    yield (
        "club_reviews.csv",
        "Club review decisions and decision timestamps.",
        select(
            ClubReview.id.label("review_id"), ClubReview.club_id, ClubReview.decision, ClubReview.reviewed_at,
        )
        .join(clubs, clubs.c.club_id == ClubReview.club_id)
        .join(SchoolAdmin, SchoolAdmin.account_id == ClubReview.admin_account_id)
        .where(SchoolAdmin.school_id == school_id)
        .order_by(ClubReview.id),
    )
    yield (
        "activities.csv",
        "Activities by club. Titles, dates, and statuses appear in activity_revisions.csv.",
        select(activities).order_by(activities.c.activity_id),
    )
    yield (
        "activity_revisions.csv",
        "Activity revision history, including status, publication, and content retention timestamps.",
        select(revisions).order_by(revisions.c.activity_id, revisions.c.revision_number),
    )
    yield (
        "activity_reviews.csv",
        "Activity review decisions and decision timestamps by revision.",
        select(
            ActivityReview.id.label("review_id"),
            ActivityReview.revision_id,
            ActivityReview.decision,
            ActivityReview.reviewed_at,
        )
        .join(revisions, revisions.c.revision_id == ActivityReview.revision_id)
        .join(Account, Account.id == func.coalesce(
            ActivityReview.represented_account_id, ActivityReview.actor_account_id
        ))
        .where(Account.school_id == school_id)
        .order_by(ActivityReview.id),
    )
    yield (
        "activity_attachments.csv",
        "Attachment types, sizes when available, and upload timestamps by revision.",
        select(
            ActivityAttachment.id.label("attachment_id"),
            ActivityAttachment.revision_id,
            ActivityAttachment.file_type,
            ActivityAttachment.size_bytes,
            ActivityAttachment.uploaded_at,
        )
        .join(revisions, revisions.c.revision_id == ActivityAttachment.revision_id)
        .where(revisions.c.content_purged_at.is_(None))
        .order_by(ActivityAttachment.id),
    )
    system_rejections = (
        select(
            ActivityDeadlineRejection.revision_id,
            ActivityDeadlineRejection.closure_request_id,
            literal("SYSTEM").label("source"),
            literal(ActivityRevisionStatus.REJECTED.value).label("decision"),
            ActivityDeadlineRejection.reason,
            ActivityDeadlineRejection.resolved_at,
        )
        .join_from(ActivityDeadlineRejection, revisions, revisions.c.revision_id == ActivityDeadlineRejection.revision_id)
        .join(AcademicYearClosureRequest, AcademicYearClosureRequest.id == ActivityDeadlineRejection.closure_request_id)
        .where(
            AcademicYearClosureRequest.school_id == school_id,
            AcademicYearClosureRequest.academic_year_id == year_id,
        )
    )
    if db.scalar(select(system_rejections.exists())):
        yield (
            "activity_deadline_rejections.csv",
            "System-generated rejections at the academic-year completion deadline.",
            system_rejections.order_by(ActivityDeadlineRejection.revision_id),
        )


def _csv_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, bool):
        return str(value).lower()
    if not isinstance(value, str):
        return value

    value = UNSAFE_CONTROLS.sub("", value)
    # Inspect forms that spreadsheets can fold or ignore before the formula prefix.
    for char in normalize("NFKC", value):
        if char in "=+-@\t\r\n":
            return "'" + value
        if not char.isspace() and category(char) != "Cf":
            break
    return value


def _check_deadline(deadline: float) -> None:
    if monotonic() >= deadline:
        raise AcademicYearError(503, "Academic-year export timed out.")


def _size_error() -> AcademicYearError:
    return AcademicYearError(413, "This academic year exceeds the export size limit.")


def _write_csv(
    db: Session,
    archive: ZipFile,
    filename: str,
    query: Select,
    *,
    remaining_rows: int,
    remaining_bytes: int,
    deadline: float,
) -> tuple[list[str], int, int]:
    _check_deadline(deadline)
    query = query.limit(remaining_rows + 1).execution_options(yield_per=FETCH_ROWS)
    with db.execute(query) as result, archive.open(filename, "w") as entry, StringIO(newline="") as buffer:
        columns = list(result.keys())
        writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        byte_count = 0
        for row_count, row in enumerate(chain((columns,), result)):
            _check_deadline(deadline)
            if row_count > remaining_rows:
                raise _size_error()
            writer.writerow(_csv_value(value) for value in row)
            encoded = buffer.getvalue().encode("utf-8")
            byte_count += len(encoded)
            if byte_count > remaining_bytes:
                raise _size_error()
            entry.write(encoded)
            buffer.seek(0)
            buffer.truncate()
    return columns, row_count, byte_count


def _write_archive(db: Session, output: BinaryIO, school_id: UUID, year_id: UUID) -> tuple[datetime, int]:
    deadline = monotonic() + MAX_EXPORT_SECONDS
    files = []
    total_rows = total_bytes = 0
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=1, allowZip64=False) as archive:
        for filename, description, query in _export_queries(db, school_id, year_id):
            columns, rows, size = _write_csv(
                db, archive, filename, query,
                remaining_rows=MAX_EXPORT_ROWS - total_rows,
                remaining_bytes=MAX_EXPORT_BYTES - total_bytes,
                deadline=deadline,
            )
            files.append({"name": filename, "description": description, "columns": columns, "rows": rows})
            total_rows += rows
            total_bytes += size

        generated_at = db.scalar(select(func.clock_timestamp())).astimezone(timezone.utc)
        manifest = {
            "schema_version": 1,
            "generated_at": generated_at.isoformat(),
            "scope": {"school_id": str(school_id), "academic_year_id": str(year_id)},
            "files": files,
        }
        encoded_manifest = json.dumps(manifest, indent=2).encode("utf-8") + b"\n"
        if total_bytes + len(encoded_manifest) > MAX_EXPORT_BYTES:
            raise _size_error()
        archive.writestr("manifest.json", encoded_manifest)

    output.flush()
    _check_deadline(deadline)
    return generated_at, total_rows


def _require_exportable_year(db: Session, school_id: UUID, year_id: UUID) -> AcademicYearStatus:
    year_status = db.scalar(select(AcademicYear.status).where(
        AcademicYear.id == year_id, AcademicYear.school_id == school_id,
    ))
    if year_status is None:
        raise AcademicYearError(404, "Academic year not found.")
    return year_status


def _reserve_export(db: Session, school_id: UUID) -> None:
    attempt = insert(AcademicYearExportLimit).values(
        school_id=school_id, last_started_at=func.statement_timestamp(),
    )
    reserved = db.scalar(attempt.on_conflict_do_update(
        index_elements=[AcademicYearExportLimit.school_id],
        set_={"last_started_at": attempt.excluded.last_started_at},
        where=AcademicYearExportLimit.last_started_at
        <= attempt.excluded.last_started_at - timedelta(seconds=EXPORT_COOLDOWN_SECONDS),
    ).returning(AcademicYearExportLimit.school_id))
    if reserved is None:
        raise AcademicYearError(429, "Wait before requesting another academic-year export for this school.")


@contextmanager
def open_academic_year_export(
    db: Session, admin: Account, year_id: UUID
) -> Iterator[tuple[BinaryIO, int]]:
    with ExitStack() as resources:
        try:
            db.execute(text("SET LOCAL statement_timeout = '10s'"))
            actor_id = admin.id
            school_id = require_admin_school(db, admin)
            _require_exportable_year(db, school_id, year_id)
            _reserve_export(db, school_id)
            # Persist the cooldown for failed attempts and release the authentication lock.
            db.commit()

            with db.begin():
                db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
                db.execute(text("SET LOCAL statement_timeout = '10s'"))
                db.execute(text("SET LOCAL idle_in_transaction_session_timeout = '30s'"))
                key = func.hashtextextended(f"clubos:academic-year-export:{school_id}", 0)
                if not db.scalar(select(func.pg_try_advisory_xact_lock(key))):
                    raise AcademicYearError(429, "An academic-year export is already running for this school.")

                current_admin = db.get(Account, actor_id, populate_existing=True)
                if current_admin is None or require_admin_school(db, current_admin) != school_id:
                    raise AcademicYearError(403, "Permission denied.")
                snapshot_status = _require_exportable_year(db, school_id, year_id)

                output = resources.enter_context(TemporaryFile(mode="w+b"))
                generated_at, total_rows = _write_archive(db, output, school_id, year_id)
                length = output.tell()
                output.seek(0)
                export_id = uuid4()
                db.add(AuditLog(
                    id=export_id,
                    actor_account_id=actor_id,
                    action=EXPORT_ACTION,
                    entity_type="academic_year",
                    entity_id=str(year_id),
                    details={"school_id": str(school_id), "row_count": total_rows},
                    occurred_at=generated_at,
                ))
                if snapshot_status == AcademicYearStatus.CLOSED and db.get(AcademicYearFinalExport, year_id) is None:
                    # The per-school export lock serializes the first receipt. The
                    # report snapshot, not a later status read, qualifies the ZIP.
                    db.add(AcademicYearFinalExport(
                        academic_year_id=year_id, school_id=school_id, export_id=export_id,
                        generated_at=generated_at, exported_by_account_id=actor_id,
                    ))
        except (
            SQLAlchemyError, OSError, ValueError, LookupError, csv.Error, BadZipFile, LargeZipFile, CompressionError,
        ) as exc:
            logger.error("Academic-year export failed (%s)", type(exc).__name__)
            raise AcademicYearError(503, "Academic-year export is temporarily unavailable.") from exc
        finally:
            db.rollback()

        yield output, length
