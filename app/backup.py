import argparse
import sys
from pathlib import Path

import psycopg
from sqlalchemy.exc import SQLAlchemyError

from app.config.settings import settings
from app.services.backups import BackupError, create_backup, prune_backups, restore_backup, verify_backup
from app.services.maintenance import MaintenanceError, exclusive_maintenance
from app.services.storage import StorageError, get_attachment_storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create, verify, prune, or rehearse restoration of private backups.")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--root", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("backup", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--target-db", required=True)
    restore.add_argument("--target-storage", type=Path, required=True)
    prune = commands.add_parser("prune")
    prune.add_argument("--root", type=Path, required=True)
    prune.add_argument("--batch-size", type=int, default=30)
    prune.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.command == "verify":
            manifest = verify_backup(args.backup)
            print(f"Backup verified with {manifest.attachment_count} attachments.")
        elif args.command == "restore":
            manifest = restore_backup(args.backup, args.target_db, args.target_storage)
            print(f"Restored and verified {manifest.attachment_count} attachments in a new disposable database.")
        else:
            storage = get_attachment_storage()
            with exclusive_maintenance() as db:
                if args.command == "create":
                    archive = create_backup(db, storage, args.root, settings.APP_ENV)
                    print(f"Created verified backup {archive.name}.")
                else:
                    count = prune_backups(db, storage, args.root, settings.APP_ENV, args.batch_size, args.dry_run)
                    print(f"Backups due for pruning: {count}." if args.dry_run else f"Pruned {count} backups.")
    except (BackupError, MaintenanceError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, StorageError, SQLAlchemyError, psycopg.Error) as exc:
        print(f"Backup operation stopped ({type(exc).__name__}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
