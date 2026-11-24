# School Admin impersonation

Impersonation belongs to one authenticated tab. The administrator remains the authenticated actor, and the selected same-school student or teacher is the represented account. An administrator may have one active impersonation across all tabs. Other authenticated tabs keep their own access.

## API

Send the existing HttpOnly session cookie and `X-ClubOS-Tab` header. Responses containing impersonation information use `Cache-Control: private, no-store`.

| Method and route | Purpose |
| --- | --- |
| `GET /admin/impersonation/eligible-accounts` | Find active students and teachers in the assigned administrator’s school. Use `q` for name search, `limit` from 1 to 50, and `after` from the prior page’s `next_cursor`. |
| `POST /admin/impersonation/start` | Start with `target_account_id`, a non-empty `reason`, and a UUID `Idempotency-Key`. Retrying the same request returns the same impersonation record. |
| `GET /admin/impersonation/current` | Get the status of the requesting tab’s impersonation for the banner. |
| `POST /admin/impersonation/end` | End the specified `impersonation_session_id`, with an optional reason. A retry identifies the same record even after a newer impersonation starts. |
| `GET /admin/impersonation/published-activities` | View the school’s current published activities as the represented student or teacher. This is the only permitted impersonated business operation. |

Ordinary protected routes require direct authentication. The administrator ends impersonation before using academic-year, account, or other administrative operations from that tab. The existing session heartbeat and logout support impersonating tabs. The parent session’s inactivity deadline applies, and logout ends its impersonation. Authorization checks record invalidation when an expired or revoked parent, an ineligible target, or loss of administrator authorization is detected. Existing impersonation history remains linked to any historical reviews.

## Authorization and audit

The database binds each new impersonation record to the administrator’s authenticated session and records its original school. Requests recheck both accounts and the administrator’s assignment. The parent session lock serializes actions on a tab with logout and termination. If inactivity renewal commits, authorization reacquires the parent lock and checks the full context again before admitting the action. The administrator row and the partial unique index serialize competing starts across tabs. A successful published-activity read records an audit event before releasing these locks. Start and terminal events include the actual administrator, represented account, and impersonation identifier. Free-text reasons live in the impersonation record. Audit events carry coded reasons and follow the existing 30-day audit retention policy.

Activity reviews remain available only to directly authenticated teachers. A future approved teacher-review capability must explicitly use the actor-aware context and record the administrator in `actor_account_id`, the target teacher in `represented_account_id`, the active `impersonation_session_id`, and `IMPERSONATION` in `review_mode`. The existing review foreign key verifies this combination. The existing year, club, revision, membership, and deadline checks still apply to any future reviewed scope.

## Deployment

Apply migration `c4d15a9e7b62` with `alembic upgrade head` before serving the new API. The migration retains legacy impersonation rows and their review references, and audits the invalidation of active rows that lack a parent tab binding. Downgrade refuses to discard tab-bound impersonation history. The existing session settings cover this workflow.
