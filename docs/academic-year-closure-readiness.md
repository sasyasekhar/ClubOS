# Academic-year closure readiness

`GET /academic-years/{year_id}/closure-readiness` uses the existing session cookie and `X-ClubOS-Tab` header. Access requires the assigned school administrator. Unknown years and years belonging to another school both return `404`.

- `academic_year_id` and `status` identify the selected year.
- `counts` reports revision totals for `PENDING`, `DRAFT`, `CHANGES_REQUESTED`, `APPROVED`, and `REJECTED`, including zero totals.
- `can_close` is true when the year is `ACTIVE` and has zero pending revisions. Pending revisions from inactive clubs also block readiness. Drafts and change-requested revisions remain unfinished history. Approved and rejected revisions are resolved, and existing review validation requires an explanation for rejection.
- `pending_review_blocker_count` gives the exact pending total. `pending_review_blockers` contains up to 50 entries ordered by `revision_id`, each with `club_id`, `activity_id`, and `revision_id`. A count greater than the list length means additional blockers exist.

The endpoint reads the year status, counts, and blocker entries from one SQL statement snapshot. Counts come from revisions joined through activities and clubs in the selected school and year. Review history can contain multiple rows for a revision, so it is excluded from the count query.

## Closure workflow

| Method and path | Access and inputs |
| --- | --- |
| `GET /academic-years/closure-teacher` | An assigned school administrator can read their school's `teacher_account_id`. The value is `null` until the school has a designation. |
| `PUT /academic-years/closure-teacher` | An assigned school administrator sets `teacher_account_id` to an active teacher from the same school. A designation applies across years, and each change produces an audit event. |
| `POST /academic-years/{year_id}/closure-request` | The designated active teacher sends a UUID `Idempotency-Key`. A successful request returns `201` with the request ID and `PENDING` status. An exact replay returns the stored request. |
| `GET /academic-years/{year_id}/closure-request` | An assigned school administrator can read the closure request for their selected year. The response is `null` until a request exists. |
| `POST /academic-years/{year_id}/confirm-closure` | An assigned school administrator supplies `request_id` in the body. A successful confirmation returns `CONFIRMED` and records `CLOSED` on the year. Repeating the same confirmation returns the recorded result. |

Initiation and confirmation return `409` with `pending_review_blocker_count` when pending activity revisions block closure. Confirmation records the year status, request status, confirming administrator, timestamp, and audit event in one transaction. It preserves the school's existing academic records.

## Concurrency at closure confirmation

The readiness response is a point-in-time observation. A student can submit a draft after a zero-blocker check, and a teacher can resolve a pending review while a confirmation is waiting. Submission and review take a shared year lock before locking the club and revision. Initiation and confirmation coordinate with those writers through the same year row. Locking existing pending revisions leaves draft-to-pending transitions and new rows possible.

The workflow follows this PostgreSQL locking protocol.

1. Resolve the authorized school and year from scoped database relationships. For normal writes that require an active year, take `FOR SHARE` on that year before locking any club or revision. Recheck `ACTIVE` on the locked row, then validate the club, activity, revision, and authorization in their current scope. Hold all locks through commit or rollback. Shared year locks allow writers in different clubs to proceed concurrently. [PostgreSQL row-level locking](https://www.postgresql.org/docs/17/explicit-locking.html#LOCKING-ROWS)
2. Initiation share-locks the school's designation, then exclusively locks the year and checks pending revisions before inserting a request. Confirmation locks the school and school-admin assignment, takes `FOR UPDATE` on the year, and locks the specified request. Run the fresh, school-scoped blocker query in a separate statement after obtaining the locks. `READ COMMITTED` lets it see submissions and reviews committed while confirmation waited. A writer waiting behind successful confirmation sees `CLOSED` when it acquires the year lock and rejects activity writes that require `ACTIVE`. [PostgreSQL transaction isolation](https://www.postgresql.org/docs/17/transaction-iso.html#XACT-READ-COMMITTED)
3. Keep the order consistent across writers. If an operation needs a school or school-admin row lock, acquire it before the year lock, including locks implied by foreign keys on insert. Acquire the year before any closure-request, club, and revision rows the operation needs. The guard covers submission and review, draft creation and edits, correction and replacement drafts, attachment uploads, club registration and reviews, and representative changes. Future jobs that can change revision status or move records into or out of a year must use the same guard. With multi-year operations, acquire year locks in a consistent order and revalidate ownership before writing.
4. Deploy the writer guards everywhere and let older writer transactions finish before enabling confirmation. The PostgreSQL integration tests exercise competing confirmations and both orderings of confirmation versus activity submission with independent connections.

The existing ZIP export takes its own reporting snapshot. Its advisory lock serializes exports and has a separate purpose from the year lock. Attachment reconciliation and superseded-content retention currently leave revision status and year ownership intact. Their operation after closure depends on the lifecycle policy.

## Product decisions still needed

The workflow preserves a pending request if confirmation encounters blockers or the requester's designation has changed or the account is inactive. It returns a conflict and leaves the year active. Reappointing a teacher creates a fresh designation. The lifecycle for requests whose authorization has changed still needs approval, as does any policy to supersede a request when blockers appear after initiation.

Resolving pending reviews in inactive clubs still needs a policy. Define retention and lifecycle rules for `CLOSED` and `ARCHIVED` years before implementing archiving, rollover, or reset.
