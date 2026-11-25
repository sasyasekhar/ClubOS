import json
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import islice

from sqlalchemy import Engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config.database import engine
from app.models.activity_attachment import ActivityAttachment
from app.models.maintenance_state import MaintenanceState
from app.services.activity_attachments import OBJECT_KEY, OBJECT_PREFIX, has_managed_object, read_verified_object, reconcile_orphaned_upload
from app.services.storage import AttachmentInventory, ObjectMissing, StorageError


PAIRING_KEY = "clubos-storage-binding"
MAINTENANCE_LOCK = 893275030417578502


class MaintenanceError(Exception):
    pass


@contextmanager
def exclusive_maintenance(database: Engine = engine) -> Iterator[Session]:
    """Pin the connection so a lock survives multiple bounded transactions."""
    with database.connect() as connection:
        locked = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": MAINTENANCE_LOCK})
        connection.commit()
        if not locked:
            raise MaintenanceError("Another maintenance or backup command holds the database lock.")
        try:
            with Session(bind=connection, autoflush=False) as db:
                yield db
        finally:
            connection.rollback()
            if not connection.invalidated:
                try:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MAINTENANCE_LOCK})
                    connection.commit()
                except SQLAlchemyError:
                    connection.invalidate()
                    raise


def require_matching_environment(db: Session, environment: str) -> None:
    name = db.scalar(text("SELECT current_database()"))
    is_test = name == "clubos_test" or name.startswith("clubos_restore_")
    if is_test != (environment == "test"):
        raise MaintenanceError("Database and environment must agree on test isolation.")


def _pairing_content(state: MaintenanceState, environment: str) -> bytes:
    return json.dumps({"identity": str(state.storage_identity), "environment": environment}, sort_keys=True).encode("ascii")


def _read_pairing(storage: AttachmentInventory) -> bytes | None:
    try:
        with storage.get(PAIRING_KEY) as source:
            return source.read(256)
    except ObjectMissing:
        return None


def validate_storage_pairing(db: Session, storage: AttachmentInventory, environment: str) -> MaintenanceState:
    require_matching_environment(db, environment)
    state = db.get(MaintenanceState, 1)
    if state is None or state.storage_environment != environment or _read_pairing(storage) != _pairing_content(state, environment):
        raise MaintenanceError("Database and private attachment storage are not paired for this environment.")
    return state


def attachment_inventory(db: Session) -> Iterator[ActivityAttachment]:
    query = select(ActivityAttachment).order_by(ActivityAttachment.storage_key.collate("C")).execution_options(yield_per=100)
    with db.execute(query) as rows:
        yield from rows.scalars()


def bind_storage(db: Session, storage: AttachmentInventory, environment: str) -> None:
    require_matching_environment(db, environment)
    state = db.get(MaintenanceState, 1)
    if state is None or (state.storage_environment is not None and state.storage_environment != environment):
        raise MaintenanceError("Database is already assigned to another environment.")
    marker = _pairing_content(state, environment)
    existing = _read_pairing(storage)
    if existing is not None and existing != marker:
        raise MaintenanceError("Private attachment storage already belongs to another database.")
    if state.storage_environment == environment:
        validate_storage_pairing(db, storage, environment)
        return

    verified = 0
    for attachment in attachment_inventory(db):
        if not has_managed_object(attachment):
            raise MaintenanceError("An existing attachment lacks verified metadata.")
        read_verified_object(storage, attachment.storage_key, attachment.size_bytes, attachment.content_sha256)
        verified += 1
    if not verified and next(iter(storage.iter_keys(OBJECT_PREFIX)), None) is not None:
        raise MaintenanceError("Unreferenced storage requires review before pairing an empty database.")
    if existing is None:
        if not storage.put_if_absent(PAIRING_KEY, marker, "application/json") and _read_pairing(storage) != marker:
            raise MaintenanceError("Private attachment storage was paired with another database.")
    state.storage_environment = environment
    db.commit()


def reconcile_attachment_batch(
    db: Session, storage: AttachmentInventory, *, limit: int, dry_run: bool = False,
) -> dict[str, int]:
    cursor = db.get(MaintenanceState, 1).orphan_cursor
    keys = list(islice(storage.iter_keys(OBJECT_PREFIX, after=cursor), limit))
    managed = [key for key in keys if OBJECT_KEY.fullmatch(key)]
    referenced = set(db.scalars(select(ActivityAttachment.storage_key).where(ActivityAttachment.storage_key.in_(managed))))
    db.rollback()
    summary = {"attachment_keys_examined": len(keys), "orphan_objects_removed": 0, "orphan_failures": 0}
    if dry_run:
        summary["unreferenced_attachment_keys"] = len(set(managed) - referenced)
        return summary
    for key in managed:
        if key in referenced:
            continue
        try:
            db.execute(text("SET LOCAL statement_timeout = '10s'"))
            summary["orphan_objects_removed"] += reconcile_orphaned_upload(db, storage, key)
        except (StorageError, SQLAlchemyError):
            db.rollback()
            summary["orphan_failures"] += 1
    state = db.get(MaintenanceState, 1)
    state.orphan_cursor = keys[-1] if len(keys) == limit else None
    db.commit()
    return summary
