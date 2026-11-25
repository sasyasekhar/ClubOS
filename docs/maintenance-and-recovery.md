# ClubOS maintenance and recovery

## Initialize a matched environment

Run from the repository root with PostgreSQL 17 client utilities on `PATH`. Use the existing `DB_*` settings and a dedicated private directory for `ATTACHMENT_STORAGE_DIRECTORY`. Set `APP_ENV=test` for `clubos_test` and `clubos_restore_…` databases. Use `APP_ENV=development` for local development. Stop older app and maintenance processes during the Day 26 migration so every new write uses the durable receipts and scan checks.

```sh
alembic upgrade head
alembic check
python -m app.maintenance bind-storage
python -m app.maintenance run --batch-size 100 --dry-run
python -m app.maintenance run --batch-size 100
```

Binding verifies existing referenced objects by key, size, and SHA-256 and writes a private pairing marker. The maintenance, backup creation, and pruning commands check the environment and database identity before touching storage. Binding an existing environment requires access to all referenced managed objects. Investigate missing files, earlier unverified metadata, or an unexpected marker before proceeding.

## Retention and scheduling

Run one maintenance batch every 10 minutes once the production environment is approved. Use the JSON counts and exit status for monitoring. The batch size applies to each queue and to each academic-year completion window examined. A partial object failure returns a nonzero status. Successful work commits at each queue's established transaction boundary and subsequent runs retry remaining work. A PostgreSQL advisory lock coordinates this command with backup creation and pruning on the same database.

Audit and login history become eligible at 30 elapsed days. Maintenance also removes authentication sessions that expired at least 30 days ago and have no impersonation references. Review records and their historical impersonation and parent references remain available for attribution. A receipt retains only the operation ID, action, fingerprint, and any result reference needed for replay. The migration removes redundant audit payload snapshots after backfilling fingerprints. Earlier audit rows whose request payload has already been scrubbed become conflict receipts.

An approved replacement removes the old revision from publication immediately. Eligible revision content and verified attachment metadata are removed at 30 elapsed days. Failed revisions rotate behind other due work. The orphan pass advances through a persistent key cursor and acquires the existing revision lock before removing unreferenced objects. Unverified legacy objects require operator review. Dry runs show a bounded preview and keep the cursor in place.

The indexed audit, login, and session queries lock one bounded batch with `SKIP LOCKED`. Revision cleanup uses the existing superseded-time index to select due rows, then orders candidates by their last failed attempt or superseded time. Storage inventory reconciliation checks each batch's known references in one query and rechecks unreferenced keys under their revision locks. The local inventory sorts siblings within each directory to resume by key.

## Create and verify a complete backup

Choose a separate, owner-only directory on private storage. The example path is excluded from Git. Use an encrypted backup mount for real data.

```sh
install -d -m 700 .clubos-backups
python -m app.backup create --root .clubos-backups
python -m app.backup verify .clubos-backups/<backup-directory>
python -m app.backup prune --root .clubos-backups --batch-size 30 --dry-run
python -m app.backup prune --root .clubos-backups --batch-size 30
```

Creation exports a single repeatable-read PostgreSQL snapshot for `pg_dump` and the referenced attachment inventory. Use the coordinated maintenance command for deletion and orphan processing so the same advisory lock keeps snapshot objects available until the copy finishes. Suspend any older manual cleanup, external bucket lifecycle deletion, or object replacement while backups run. Uploads can continue because committed objects use unique keys.

The completed directory contains the custom-format database archive, a manifest, and a streamed index of object keys, sizes, and hashes. It is published by rename after verification. Verification checks the archive checksum and directory listing, the inventory checksum, and every copied object's bytes. A missing object or missing verifiable attachment metadata fails the backup. The process reads at most one attachment into memory at a time, bounded by 10 MiB, and streams hashes for the database archive. It holds the database snapshot until the referenced objects have been copied. Plan disk headroom for the database archive, attachments, and the next complete backup. [PostgreSQL 17 pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)

Database credentials come from the usual private settings and reach the PostgreSQL tools through a temporary mode-0600 password file. Keep backup folders private because the archive includes password hashes and private records. Pruning uses PostgreSQL time and removes completed backups for the paired database at the 30-day boundary. Monitor daily creation and prune separately after a verified copy. Apply the same expiry to replicated copies. [PostgreSQL password file](https://www.postgresql.org/docs/17/libpq-pgpass.html)

## Rehearse restoration in isolation

Use an access-controlled copy from a trusted ClubOS deployment. Use matching application code and an isolated local PostgreSQL cluster. Keep restored data offline until checks finish. The restore tool requires a new `clubos_restore_…` database name and a new storage directory beneath a private parent. The database role needs permission to create that disposable database.

```sh
install -d -m 700 .clubos-restore
python -m app.backup restore .clubos-backups/<backup-directory> \
  --target-db clubos_restore_drill --target-storage .clubos-restore/drill
APP_ENV=test DB_NAME=clubos_restore_drill ATTACHMENT_STORAGE_DIRECTORY=.clubos-restore/drill \
  python -m app.maintenance run --dry-run
APP_ENV=test DB_NAME=clubos_restore_drill ATTACHMENT_STORAGE_DIRECTORY=.clubos-restore/drill \
  python -m app.maintenance run
```

Restoration checks the Alembic head and schema, school membership links, and every attachment against the restored metadata. It revokes all restored login sessions, rotates their token hashes, expires active impersonation sessions, and pairs the new private storage. The review rows retain their references. Run maintenance in bounded passes until overdue completion windows, history, recovery markers, and revision content have been reconciled. Review partial failures before serving requests. Verify representative business data, academic-year exports, and an authorized scanned attachment in each restore drill. [PostgreSQL 17 pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html)

Run a production restore drill on a separate, restricted environment before the release and periodically thereafter. A failed drill leaves the new database and storage destination available for inspection. A production cutover requires explicit approval, trusted network access, encrypted and separately access-controlled backup storage, monitoring, and a reviewed rollback plan.

## Draft recovery and production decisions

The internal draft recovery service records school, original revision, actual actor, timestamps, and a retry-safe lifecycle. Its eligibility checks cover tenant scope, current write deadlines, original draft state, and retained content. Expiration removes the recoverable reference from the marker. Draft visibility and other user-facing deletion and restoration behavior require product approval.

Approve who may delete a draft, who may restore it, how a pending deletion hides it, and how an approved restoration makes it visible. Decide whether other entities need recovery individually. Membership, school configuration, and credentials each require different business rules.

Production requires the approved private attachment provider, verified scanning of existing files, the Unix socket and signature updater for ClamAV, an exact HTTPS `PUBLIC_ORIGIN`, and a reverse proxy that trusts forwarded client information only from configured infrastructure. Secure cookies are enforced in production. Plan the maintenance schedule, encrypted backup destination, and restore drills before opening the system to users. [Private attachments and scanning](activity-attachments.md)
