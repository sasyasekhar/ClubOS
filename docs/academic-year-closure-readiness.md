# Academic-year closure readiness

`GET /academic-years/{year_id}/closure-readiness` uses the existing session cookie and `X-ClubOS-Tab` header. Access requires the assigned school administrator. Unknown years and years belonging to another school both return `404`.

- `academic_year_id` and `status` identify the selected year.
- `counts` reports revision totals for `PENDING`, `DRAFT`, `CHANGES_REQUESTED`, `APPROVED`, and `REJECTED`, including zero totals.
- `can_initiate` identifies an `ACTIVE` year with no closure request. Initiation requires the current designated teacher, and pending reviews are included in monitoring from the start of the window.
- `window_status` is `NOT_REQUESTED`, `OPEN`, `EXPIRED`, `FINALIZED`, or `CONFIRMED`. `completion_deadline_at` and `finalized_at` show the cutoff and processing time. `can_confirm` becomes true for an active year with an expired request, including when confirmation still needs to reconcile pending reviews. `can_close` is an alias for `can_confirm`.
- `pending_review_blocker_count` gives the exact pending total. `pending_review_blockers` contains up to 50 entries ordered by `revision_id`, each with `club_id`, `activity_id`, and `revision_id`. `pending_review_attention_count` counts pending reviews in inactive clubs or without an eligible teacher. `auto_rejected_count` counts recorded system rejections.

The endpoint reads the year status, counts, and blocker entries from one SQL statement snapshot. Counts come from revisions joined through activities and clubs in the selected school and year. Review history can contain multiple rows for a revision, so it is excluded from the count query.

## Closure workflow

| Method and path | Access and inputs |
| --- | --- |
| `GET /academic-years/closure-teacher` | An assigned school administrator can read their school's `teacher_account_id`. The value is `null` until the school has a designation. |
| `PUT /academic-years/closure-teacher` | An assigned school administrator sets `teacher_account_id` to an active teacher from the same school. A designation applies across years, and each change produces an audit event. |
| `POST /academic-years/{year_id}/closure-request` | The designated active teacher sends a UUID `Idempotency-Key`. A successful request returns `201` with the request ID, `PENDING` status, and original deadline. A replay returns the same request and its current status. A distinct duplicate returns `409` with code `ACADEMIC_YEAR_CLOSURE_REQUEST_EXISTS`, the request ID, and existing deadline. |
| `GET /academic-years/{year_id}/closure-request` | An assigned school administrator can read the closure request for their selected year. The response is `null` until a request exists. |
| `GET /academic-years/{year_id}/pending-review-alerts` | Teachers see their assigned pending reviews during an open window. The School Admin sees school-wide pending reviews for the year and can use `attention_only=true` to find work without an eligible reviewer. Use `limit` (1–50) and `after` from the response's `next_cursor` to page. |
| `POST /academic-years/{year_id}/confirm-closure` | An assigned school administrator supplies `request_id` in the body. Before the deadline, the API returns `409` with code `ACADEMIC_YEAR_COMPLETION_WINDOW_OPEN`. A successful confirmation after the deadline returns `CONFIRMED` and records `CLOSED` on the year. Repeating the same confirmation returns the recorded result. |

A request opens a 168-hour window from its stored PostgreSQL timestamp. The year remains `ACTIVE` while students and teachers complete eligible work. Alert polling reads current pending work without creating records, and reviews disappear from the list when resolved. Teacher reassignment or deactivation preserves the original request and deadline. Only the currently designated active teacher can initiate a new request.

At the deadline, the backend rejects new academic-year activity and club mutations with `409` and code `ACADEMIC_YEAR_COMPLETION_WINDOW_ENDED`. Read-only historical access remains available. Finalization rejects still-pending revisions with the reason `Academic-year deadline expired` and stores separate system-action history. It preserves manual reviews, existing publications, attachments, and unfinished drafts and change requests. Exports include `activity_deadline_rejections.csv` when the selected year has system rejections.

Confirmation reconciles overdue work if necessary and records the year status, request status, confirming administrator, timestamp, and audit event in the same transaction.

## Concurrency at closure confirmation

Readiness is a point-in-time observation. Submission and review take a shared year lock before locking the club and revision. Initiation, finalization, and confirmation coordinate with those writers through the same year row.

The workflow follows this PostgreSQL locking protocol.

1. Writers take `FOR SHARE` on the scoped year before acquiring club or revision locks. They check PostgreSQL's current time after obtaining the locks needed for mutation. A write admitted before the cutoff can commit while holding its shared lock, and the exclusive finalization lock waits for it. [PostgreSQL row-level locking](https://www.postgresql.org/docs/17/explicit-locking.html#LOCKING-ROWS)
2. Initiation share-locks the school's designation and exclusively locks the year. Finalization locks the year `FOR UPDATE`, then the request. Confirmation first locks the school and school-admin assignment, then takes the same exclusive year and request locks. Fresh `READ COMMITTED` queries see submissions and reviews committed while the exclusive lock was waiting. [PostgreSQL transaction isolation](https://www.postgresql.org/docs/17/transaction-iso.html#XACT-READ-COMMITTED)
3. Keep the order consistent across writers. If an operation needs a school or school-admin row lock, acquire it before the year lock, including locks implied by foreign keys on insert. Acquire the year before any closure-request, club, and revision rows the operation needs. The guard covers submission and review, draft creation and edits, correction and replacement drafts, attachment uploads, club registration and reviews, and representative changes. Future jobs that can change revision status or move records into or out of a year must use the same guard. With multi-year operations, acquire year locks in a consistent order and revalidate ownership before writing.
4. Deploy the writer guards everywhere and let older writer transactions finish before running the finalizer or enabling confirmation. The PostgreSQL integration tests exercise competing finalizers, competing confirmations, and the ordering of confirmation and activity writes with independent connections.

The existing ZIP export takes its own reporting snapshot. Its advisory lock serializes exports and has a separate purpose from the year lock. Attachment reconciliation and superseded-content retention currently leave revision status and year ownership intact. Their operation after closure depends on the lifecycle policy.

## Deploying deadline processing

Migration `92f0cb15a83e` derives deadlines for existing requests from their original timestamps. Previously confirmed requests retain their original confirmation. If a legacy pending request is already overdue, it becomes eligible for finalization after upgrade. Downgrade is guarded once finalization history exists because the old schema cannot preserve that history.

Run the worker from the application environment using the existing database settings.

```sh
python -m app.finalize_academic_years --limit 100 --batch-size 500
```

Each invocation takes one bounded batch from each selected overdue window and commits the batch before moving on. Repeated runs finish larger windows and safely retry interrupted work. Configure and test a recurring invocation in the production scheduler to process overdue reviews without an administrator request. Until then, backend writes still freeze at the deadline, and administrator confirmation can reconcile overdue reviews on demand.
