import argparse
import json
import sys

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config.settings import settings
from app.services.academic_year_closure_finalization import due_closure_windows, process_expired_closure_windows
from app.services.draft_recovery import expire_draft_recovery
from app.services.history_retention import cleanup_history
from app.services.maintenance import (
    MaintenanceError, bind_storage, exclusive_maintenance, reconcile_attachment_batch, validate_storage_pairing,
)
from app.services.revision_retention import preview_revision_cleanup, purge_superseded_batch
from app.services.sessions import database_now
from app.services.storage import AttachmentInventory, StorageError, get_attachment_storage


def run_maintenance(db: Session, storage: AttachmentInventory, environment: str, limit: int, dry_run: bool) -> dict:
    if not 1 <= limit <= 500:
        raise MaintenanceError("Batch size must be between 1 and 500.")
    with db.begin():
        validate_storage_pairing(db, storage, environment)
        now = database_now(db)
        if dry_run:
            windows = len(db.execute(due_closure_windows(limit)).all())
            revisions_due = preview_revision_cleanup(db, now, limit)

    summary = {"dry_run": dry_run}
    if dry_run:
        summary.update(closure_windows=windows, revisions_due=revisions_due)
    else:
        windows, rejected = process_expired_closure_windows(db, limit=limit, batch_size=limit)
        revisions = purge_superseded_batch(db, storage=storage, now=now, limit=limit)
        summary.update(closure_windows=windows, deadline_rejections=rejected,
                       revisions_purged=revisions.purged, revision_failures=revisions.failed)
    summary.update(cleanup_history(db, limit=limit, now=now, dry_run=dry_run))
    summary["recoveries_due" if dry_run else "recoveries_expired"] = expire_draft_recovery(
        db, limit=limit, now=now, dry_run=dry_run,
    )
    try:
        summary.update(reconcile_attachment_batch(db, storage, limit=limit, dry_run=dry_run))
    except StorageError:
        db.rollback()
        summary["orphan_failures"] = 1
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run ClubOS retention and closure maintenance.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bind-storage", help="Verify and pair the configured private storage and database.")
    run = commands.add_parser("run", help="Process one bounded batch of each maintenance operation.")
    run.add_argument("--batch-size", type=int, default=100)
    run.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        storage = get_attachment_storage()
        with exclusive_maintenance() as db:
            if args.command == "bind-storage":
                bind_storage(db, storage, settings.APP_ENV)
                print("Private attachment storage is paired with the database.")
                return 0
            summary = run_maintenance(db, storage, settings.APP_ENV, args.batch_size, args.dry_run)
    except MaintenanceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (StorageError, SQLAlchemyError, OSError) as exc:
        print(f"Maintenance stopped ({type(exc).__name__}).", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return int(bool(summary.get("revision_failures") or summary.get("orphan_failures")))


if __name__ == "__main__":
    raise SystemExit(main())
