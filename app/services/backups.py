import hashlib
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import zip_longest
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from typing import Annotated, Literal
from uuid import UUID, uuid4

import psycopg
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from psycopg import sql
from pydantic import AwareDatetime, BaseModel, Field, ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config.database import database_url
from app.config.settings import settings
from app.models.base import Base
from app.models.maintenance_state import MaintenanceState
from app.services.activity_attachments import OBJECT_KEY, has_managed_object, read_verified_object
from app.services.attachment_policy import MAX_FILE_SIZE
from app.services.history_retention import RETENTION_PERIOD
from app.services.maintenance import attachment_inventory, bind_storage, validate_storage_pairing
from app.services.sessions import database_now
from app.services.storage import AttachmentInventory, StorageError
from app.services.storage.local import LocalAttachmentStorage


MANIFEST = "manifest.json"
INVENTORY = "attachments.jsonl"
DUMP = "database.dump"
BACKUP_NAME = re.compile(r"backup-\d{8}T\d{6}Z-[0-9a-f]{32}")
RESTORE_DATABASE = re.compile(r"clubos_restore_[a-z0-9_]{1,40}")
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class BackupError(Exception):
    pass


class BackupManifest(BaseModel):
    version: Literal[1] = 1
    created_at: AwareDatetime
    storage_identity: UUID
    environment: Literal["development", "test", "production"]
    migration_heads: list[str] = Field(min_length=1, max_length=16)
    attachment_count: int = Field(ge=0)
    inventory_sha256: Digest
    database_sha256: Digest


class BackupObject(BaseModel):
    key: str = Field(max_length=500)
    size: int = Field(ge=1, le=MAX_FILE_SIZE)
    sha256: Digest
    media_type: str = Field(min_length=1, max_length=100)


def _private_directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    info = path.lstat()
    static_root = Path("app/static").resolve()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or path != path.resolve() or path.is_relative_to(static_root) or static_root.is_relative_to(path)):
        raise BackupError("Use a private, owner-only directory outside public static storage.")
    return path


@contextmanager
def _private_file(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise BackupError("Backup files must be private regular files.")
        yield stream


def _hash_file(path: Path) -> str:
    with _private_file(path) as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@contextmanager
def _output_file(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        yield stream
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _run_postgres_tool(arguments: list[str], *, environment: dict | None = None) -> None:
    try:
        subprocess.run(arguments, env=environment, check=True, timeout=3600,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupError(f"{arguments[0]} failed. Verify PostgreSQL client tools and access.") from exc


@contextmanager
def _postgres_environment(database_name: str):
    def escape(value: str) -> str:
        if "\n" in value or "\r" in value:
            raise BackupError("Database credentials cannot be encoded in a password file.")
        return value.replace("\\", "\\\\").replace(":", "\\:")

    with TemporaryDirectory(prefix="clubos-pgpass-") as scratch:
        password_file = Path(scratch) / "pgpass"
        with _output_file(password_file) as stream:
            stream.write(f"*:*:*:{escape(settings.DB_USER)}:{escape(settings.DB_PASSWORD)}\n".encode())
        environment = {
            key: value for key, value in os.environ.items()
            if key in {"PATH", "LANG", "LC_ALL"} or key.startswith("PGSSL") or key == "PGCHANNELBINDING"
        }
        environment.update(PGPASSFILE=str(password_file), PGHOST=settings.DB_HOST, PGPORT=str(settings.DB_PORT),
                           PGDATABASE=database_name, PGUSER=settings.DB_USER, PGCONNECT_TIMEOUT="10")
        yield environment


def _objects(directory: Path) -> Iterator[BackupObject]:
    previous = ""
    with _private_file(directory / INVENTORY) as inventory:
        while line := inventory.readline(2048):
            if len(line) >= 2048 or not line.endswith(b"\n"):
                raise BackupError("Attachment inventory contains an invalid record.")
            try:
                obj = BackupObject.model_validate_json(line)
            except ValidationError as exc:
                raise BackupError("Attachment inventory is invalid.") from exc
            if not OBJECT_KEY.fullmatch(obj.key) or obj.key <= previous:
                raise BackupError("Attachment keys are invalid, duplicated, or out of order.")
            previous = obj.key
            yield obj


def _manifest(directory: Path) -> BackupManifest:
    with _private_file(directory / MANIFEST) as stream:
        try:
            return BackupManifest.model_validate_json(stream.read(4096))
        except ValidationError as exc:
            raise BackupError("Backup manifest is invalid.") from exc


def verify_backup(directory: Path) -> BackupManifest:
    directory = _private_directory(directory)
    manifest = _manifest(directory)
    if (_hash_file(directory / DUMP) != manifest.database_sha256
            or _hash_file(directory / INVENTORY) != manifest.inventory_sha256):
        raise BackupError("Backup database or inventory checksum does not match.")
    _run_postgres_tool(["pg_restore", "--list", str(directory / DUMP)])
    storage = LocalAttachmentStorage(directory / "objects")
    count = 0
    for obj in _objects(directory):
        read_verified_object(storage, obj.key, obj.size, obj.sha256)
        count += 1
    if count != manifest.attachment_count:
        raise BackupError("Backup attachment count does not match.")
    return manifest


def _attachment_record(attachment) -> BackupObject:
    if not has_managed_object(attachment):
        raise BackupError("An attachment lacks verifiable metadata. Reconcile it before backing up.")
    return BackupObject(key=attachment.storage_key, size=attachment.size_bytes,
                        sha256=attachment.content_sha256, media_type=attachment.file_type)


def create_backup(db: Session, storage: AttachmentInventory, root: Path, environment: str) -> Path:
    """Use one exported PostgreSQL snapshot for the dump and attachment inventory."""
    root = _private_directory(root)
    staged = Path(mkdtemp(prefix=".clubos-backup-", dir=root))
    try:
        copy = LocalAttachmentStorage(staged / "objects")
        with db.begin():
            db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            state = validate_storage_pairing(db, storage, environment)
            started_at = database_now(db)
            snapshot = db.scalar(text("SELECT pg_export_snapshot()"))
            heads = sorted(MigrationContext.configure(db.connection()).get_current_heads())
            with _output_file(staged / DUMP):
                pass
            with _postgres_environment(db.scalar(text("SELECT current_database()"))) as pg_env:
                _run_postgres_tool([
                    "pg_dump", "--no-password", "--format=custom", "--no-owner", "--no-privileges",
                    f"--snapshot={snapshot}", f"--file={staged / DUMP}",
                ], environment=pg_env)
            count = 0
            with _output_file(staged / INVENTORY) as inventory:
                for attachment in attachment_inventory(db):
                    obj = _attachment_record(attachment)
                    data = read_verified_object(storage, obj.key, obj.size, obj.sha256)
                    copy.put(obj.key, data, obj.media_type)
                    inventory.write(obj.model_dump_json().encode() + b"\n")
                    count += 1
            manifest = BackupManifest(
                created_at=started_at, storage_identity=state.storage_identity, environment=environment,
                migration_heads=heads, attachment_count=count, inventory_sha256=_hash_file(staged / INVENTORY),
                database_sha256=_hash_file(staged / DUMP),
            )
        with _private_file(staged / DUMP) as dump:
            os.fsync(dump.fileno())
        with _output_file(staged / MANIFEST) as stream:
            stream.write(manifest.model_dump_json().encode() + b"\n")
        verify_backup(staged)
        _sync_directory(staged)
        destination = root / f"backup-{started_at:%Y%m%dT%H%M%SZ}-{uuid4().hex}"
        staged.rename(destination)
        _sync_directory(root)
        return destination
    finally:
        if staged.is_dir():
            shutil.rmtree(staged)


def prune_backups(db: Session, storage: AttachmentInventory, root: Path, environment: str, limit: int, dry_run: bool) -> int:
    if not 1 <= limit <= 500:
        raise BackupError("Batch size must be between 1 and 500.")
    root = _private_directory(root)
    with db.begin():
        state = validate_storage_pairing(db, storage, environment)
        identity = state.storage_identity
        cutoff = database_now(db) - RETENTION_PERIOD
    removed = 0
    with os.scandir(root) as directories:
        for candidate in directories:
            if not BACKUP_NAME.fullmatch(candidate.name) or not candidate.is_dir(follow_symlinks=False):
                continue
            path = _private_directory(Path(candidate.path))
            manifest = _manifest(path)
            if manifest.storage_identity == identity and manifest.environment == environment and manifest.created_at <= cutoff:
                if not dry_run:
                    shutil.rmtree(path)
                removed += 1
                if removed == limit:
                    break
    if removed and not dry_run:
        _sync_directory(root)
    return removed


def _create_restore_database(name: str) -> None:
    if (not RESTORE_DATABASE.fullmatch(name) or name == settings.DB_NAME or settings.APP_ENV == "production"
            or not (settings.DB_HOST in {"localhost", "127.0.0.1", "::1"} or settings.DB_HOST.startswith("/"))):
        raise BackupError("Restore verification requires a new clubos_restore_ database on an isolated local cluster.")
    with psycopg.connect(host=settings.DB_HOST, port=settings.DB_PORT, user=settings.DB_USER, dbname="postgres",
                         password=settings.DB_PASSWORD, autocommit=True, connect_timeout=10) as db:
        try:
            db.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        except psycopg.errors.DuplicateDatabase as exc:
            raise BackupError("The restore database already exists. Choose a new disposable name.") from exc
        db.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))


def _invalidate_restored_sessions(db: Session) -> None:
    db.execute(text("""
        UPDATE auth_sessions
        SET revoked_at = coalesce(revoked_at, clock_timestamp()),
            token_hash = encode(sha256(uuid_send(gen_random_uuid()) || uuid_send(id)), 'hex')
    """))
    db.execute(text("""
        WITH expired AS (
            UPDATE impersonation_sessions
            SET status = 'EXPIRED', ended_at = greatest(started_at, clock_timestamp()), end_reason = 'RESTORE_INVALIDATED'
            WHERE status = 'ACTIVE'
            RETURNING id, admin_account_id, target_account_id, ended_at
        )
        INSERT INTO audit_logs (id, actor_account_id, represented_account_id, impersonation_session_id,
                                action, entity_type, entity_id, details, occurred_at)
        SELECT gen_random_uuid(), admin_account_id, target_account_id, id, 'IMPERSONATION_INVALIDATED',
               'impersonation_session', id::text, jsonb_build_object('reason', 'RESTORE_INVALIDATED'), ended_at
        FROM expired
    """))
    db.commit()


def _verify_restored_schema(db: Session, manifest: BackupManifest) -> None:
    migrations = MigrationContext.configure(db.connection(), opts={"compare_type": True})
    expected = sorted(ScriptDirectory.from_config(Config("alembic.ini")).get_heads())
    if sorted(migrations.get_current_heads()) != manifest.migration_heads or expected != manifest.migration_heads:
        raise BackupError("The backup and application migration heads differ.")
    if compare_metadata(migrations, Base.metadata):
        raise BackupError("The restored schema differs from the application models.")
    mismatch = db.scalar(text("""
        SELECT EXISTS (
            SELECT 1 FROM club_memberships AS m
            JOIN clubs AS c ON c.id = m.club_id JOIN accounts AS a ON a.id = m.account_id
            WHERE a.school_id IS DISTINCT FROM c.school_id
        )
    """))
    if mismatch:
        raise BackupError("The restored database has inconsistent school memberships.")


def restore_backup(directory: Path, database_name: str, storage_root: Path) -> BackupManifest:
    directory = _private_directory(directory)
    manifest = verify_backup(directory)
    storage_root = storage_root.expanduser().absolute()
    _private_directory(storage_root.parent)
    if storage_root.is_relative_to(directory) or storage_root.exists() or storage_root.is_symlink():
        raise BackupError("Restoration requires a new private storage directory.")
    _create_restore_database(database_name)
    # A failed restore leaves the disposable destination isolated for inspection.
    storage_root.mkdir(mode=0o700)
    with _postgres_environment(database_name) as pg_env:
        _run_postgres_tool([
            "pg_restore", "--no-password", "--single-transaction", "--exit-on-error", "--no-owner",
            "--no-privileges", f"--dbname={database_name}", str(directory / DUMP),
        ], environment=pg_env)
    target = create_engine(database_url.set(database=database_name), hide_parameters=True)
    try:
        with Session(target) as db:
            _invalidate_restored_sessions(db)
            _verify_restored_schema(db, manifest)
            source = LocalAttachmentStorage(directory / "objects")
            storage = LocalAttachmentStorage(storage_root)
            for attachment, obj in zip_longest(attachment_inventory(db), _objects(directory)):
                if attachment is None or obj is None or _attachment_record(attachment) != obj:
                    raise BackupError("Restore attachment inventory differs from PostgreSQL metadata.")
                data = read_verified_object(source, obj.key, obj.size, obj.sha256)
                storage.put(obj.key, data, obj.media_type)
                read_verified_object(storage, obj.key, obj.size, obj.sha256)
            binding = db.get(MaintenanceState, 1)
            binding.storage_identity = uuid4()
            binding.storage_environment = None
            binding.orphan_cursor = None
            db.flush()
            bind_storage(db, storage, "test")
    finally:
        target.dispose()
    return manifest
