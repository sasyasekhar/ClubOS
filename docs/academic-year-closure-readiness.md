# Academic-year closure readiness

`GET /academic-years/{year_id}/closure-readiness` uses the existing session cookie and `X-ClubOS-Tab` header. Access requires the assigned school administrator. Unknown years and years belonging to another school both return `404`.

- `academic_year_id` and `status` identify the selected year.
- `counts` reports revision totals for `PENDING`, `DRAFT`, `CHANGES_REQUESTED`, `APPROVED`, and `REJECTED`, including zero totals.
- `can_close` is true when the year is `ACTIVE` and has zero pending revisions. Pending revisions from inactive clubs also block readiness. Drafts and change-requested revisions remain unfinished history. Approved and rejected revisions are resolved, and existing review validation requires an explanation for rejection.
- `pending_review_blocker_count` gives the exact pending total. `pending_review_blockers` contains up to 50 entries ordered by `revision_id`, each with `club_id`, `activity_id`, and `revision_id`. A count greater than the list length means additional blockers exist.

The endpoint reads the year status, counts, and blocker entries from one SQL statement snapshot. Counts come from revisions joined through activities and clubs in the selected school and year. Review history can contain multiple rows for a revision, so it is excluded from the count query.

## Concurrency at closure confirmation

The readiness response is a point-in-time observation. A student can submit a draft after a zero-blocker check, and a teacher can resolve a pending review while a confirmation is waiting. Current submission and review transactions lock the club and revision rows and read the year status without a year-row lock. Locking only the school row leaves those activity writes free to proceed. Locking existing pending revisions leaves draft-to-pending transitions and new rows possible.

Use the following PostgreSQL locking protocol in the closure checkpoint.

1. Resolve the authorized school and year from scoped database relationships. For normal writes that require an active year, take `FOR SHARE` on that year before locking any club or revision. Recheck `ACTIVE` on the locked row, then validate the club, activity, revision, and authorization in their current scope. Hold all locks through commit or rollback. Shared year locks allow writers in different clubs to proceed concurrently. [PostgreSQL row-level locking](https://www.postgresql.org/docs/17/explicit-locking.html#LOCKING-ROWS)
2. For school-admin confirmation, take `FOR UPDATE` on the year, then lock and validate the teacher's closure request. Run a fresh, school-scoped query for pending revisions in a separate statement after obtaining the locks. Use `READ COMMITTED` so this query sees submissions and reviews committed while confirmation waited. A snapshot taken before waiting could miss a submission. If the year is still `ACTIVE` and the check finds zero blockers, record the confirmation, status change, and audit event in the same transaction. A writer waiting behind successful confirmation must see `CLOSED` when it acquires the year lock and reject activity writes that require `ACTIVE`. [PostgreSQL transaction isolation](https://www.postgresql.org/docs/17/transaction-iso.html#XACT-READ-COMMITTED)
3. Keep the order consistent across writers. Acquire a school row first if an operation needs both school and year locks, then the year, then any closure-request, club, and revision rows needed by that operation. Apply the year guard to submission and review, draft creation and edits, correction and replacement drafts, attachment uploads, club registration, and other club or representative changes that must be frozen at closure. Future jobs that can change revision status or move records into or out of a year must use the same guard. With multi-year operations, acquire year locks in a consistent order and revalidate ownership before writing.
4. Deploy the writer guards everywhere and let transactions running the previous code finish before enabling confirmation. Add two-connection PostgreSQL tests for both lock acquisition orders. Cover a submission that commits while confirmation waits, a submitter that sees `CLOSED` after waiting, a reviewer that resolves a blocker while confirmation waits, and competing confirmations that produce a single transition and audit event.

The existing ZIP export takes its own reporting snapshot. Its advisory lock serializes exports and has a separate purpose from the year lock. Attachment reconciliation and superseded-content retention currently leave revision status and year ownership intact. Their operation after closure depends on the lifecycle policy.

## Product decisions still needed

Decide which teachers may request closure, how duplicate or stale requests are handled, and how pending reviews in inactive clubs can be resolved. Define which writes and retention operations are allowed for `CLOSED` and `ARCHIVED` years before implementing archiving, rollover, or reset. Preserve historical records, review decisions, audit entries, and attachments according to those explicit policies.
