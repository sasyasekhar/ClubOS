# Private activity attachments

For local development, set `APP_ENV=development` and `ATTACHMENT_STORAGE_BACKEND=local`. The default `ATTACHMENT_STORAGE_DIRECTORY` is `.clubos-attachments`, which Git ignores. Use a dedicated private directory owned by the ClubOS process. If using an absolute path, configure its real POSIX path without symlink components (for example, `/private/tmp/clubos-attachments` on macOS). Keep it outside web-served directories. ClubOS refuses to start with local storage when `APP_ENV=production`.

## API

Both endpoints require the ClubOS session cookie and `X-ClubOS-Tab` header.

| Method | Path | Behavior |
| --- | --- | --- |
| POST | `/clubs/{club_id}/activities/{activity_id}/revisions/{revision_id}/attachments` | Accepts one multipart `file` field, up to 10 MiB, and a UUID `Idempotency-Key` header. The active student representative can upload to an active club's draft in an active academic year. |
| GET | `/clubs/{club_id}/activities/{activity_id}/revisions/{revision_id}/attachments/{attachment_id}` | Serves a download to same-school students, teachers, and admins for the current published revision. For unpublished revisions, access is limited to active student and teacher representatives and the school administrator. |

Upload keys are scoped to the revision. While upload authorization still holds, an identical request returns the original 201 response, and different content or a different filename with the same key receives 409. A retry after submission receives 409. Successful responses contain the attachment ID, revision ID, filename, and media type.

ClubOS validates PDF, PNG, and JPEG structure before sending the file to the configured private scanner. A clean verdict is recorded with the file's size and SHA-256. An unsafe result returns 422. An unavailable scanner, timeout, or indeterminate result returns 503. An identical retry of a verified upload returns its existing response even while the scanner is offline.

Downloads require stored scan evidence and verify the object size and SHA-256 against PostgreSQL before sending bytes. Missing and unscanned attachments return 404. Storage errors and corrupt content return 503. A verified backup can restore a missing committed object under its existing key.

Set `ATTACHMENT_SCAN_SOCKET` to the absolute path to a private ClamAV Unix socket. Use a service account and a restricted socket group. Set `StreamMaxLength` and `MaxFileSize` to at least 10 MiB, size `MaxScanSize` for the formats permitted by your deployment, and enable `AlertExceedsMax` so limit failures produce a non-clean result. Update signatures with `freshclam`. Pre-existing files require verified scanning before they become eligible for download. [ClamAV configuration](https://docs.clamav.net/manual/Usage/Configuration.html) · [ClamD streaming protocol](https://docs.clamav.net/manual/Usage/ClamdProtocol.html)

## Retention and reconciliation

A replacement takes over when approved. The previous revision becomes unpublished immediately and stays recoverable for 30 days. At expiry, the cleanup worker locks eligible revisions, deletes their files, and then removes attachment metadata and clears revision content. Review rows and audit relationships survive. Failed or partial deletes keep the revision eligible. A subsequent run can safely delete already absent objects. Older attachment rows without verified upload metadata are preserved for operator review and excluded from private downloads.

An ambiguous database commit leaves its uploaded object in place. Inventory reconciliation acquires the upload's revision lock before checking PostgreSQL and removes only managed objects that have no metadata reference. Each upload attempt uses a fresh object key, so a later retry keeps its own object. Use `python -m app.maintenance` from the repository root to pair the database and private directory and process bounded batches. Hidden `.upload-*.tmp` files from interrupted local writes can be removed while upload processes are stopped. [Maintenance and restore runbook](maintenance-and-recovery.md)

## Connect Cloudflare R2 later

1. Enable R2 and create a private bucket for each deployment environment. Keep `r2.dev` access disabled and leave public custom domains unconfigured. [Cloudflare R2 bucket access](https://developers.cloudflare.com/r2/buckets/public-buckets/)
2. Create an R2 API token with **Object Read & Write** scoped to the deployment bucket. Store its Access Key ID and Secret Access Key in deployment secrets, along with the endpoint and bucket configuration. Use the account or jurisdiction-specific endpoint and the S3 region `auto`. [R2 authentication](https://developers.cloudflare.com/r2/api/tokens/) · [S3 compatibility](https://developers.cloudflare.com/r2/api/s3/api/)
3. Implement an R2 adapter behind `AttachmentStorage` and `AttachmentInventory` using S3 `PutObject`, `GetObject`, `DeleteObject`, and paginated `ListObjectsV2`. Close response bodies reliably, preserve object keys, and translate provider errors to `StorageError` or `ObjectMissing`. Keep the authenticated ClubOS endpoint as the download entry point. [S3 compatibility](https://developers.cloudflare.com/r2/api/s3/api/)
4. Add `r2` to the provider settings and factory and require its secrets and private bucket configuration at production startup. If there are local uploads to preserve, stop attachment writes during cutover, copy each referenced object under the same key, verify size and SHA-256, then switch providers.
5. Use a private staging bucket to verify uploads, downloads, storage timeouts, cleanup, and reconciliation before enabling production writes. Keep automated CI on its fake storage adapter.
