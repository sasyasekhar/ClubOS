# School Admin impersonation

Impersonation belongs to one authenticated tab. The administrator remains the authenticated actor, and the selected same-school student or teacher is the represented account. An administrator may have one active impersonation across all tabs. Other authenticated tabs keep their own access.

## API

Send the existing HttpOnly session cookie and `X-ClubOS-Tab` header. Responses containing impersonation information use `Cache-Control: private, no-store`.

| Method and route | Purpose |
| --- | --- |
| `GET /admin/impersonation/eligible-accounts` | Find active students and teachers in the assigned administrator’s school. Use `q` for name search, `limit` from 1 to 50, and `after` from the prior page’s `next_cursor`. |
| `POST /admin/impersonation/start` | Start with `target_account_id`, a non-empty `reason`, and a UUID `Idempotency-Key`. Retrying the same request returns the same impersonation record. |
| `GET /admin/impersonation/current` | Get the active or most recent state of the requesting tab’s impersonation. |
| `POST /admin/impersonation/end` | End the specified `impersonation_session_id`, with an optional reason. A retry identifies the same record even after a newer impersonation starts. |
| `GET /admin/impersonation/dashboard` | Get the represented member’s dashboard. Students see their memberships, representative applications, authorized activity revisions, and the school’s current publications. Teachers see their memberships, invitations, pending reviews, review history, and the same publications. |
| `GET /admin/impersonation/dashboard/clubs/{club_id}` | Get details of a club related to the represented member through an active assignment, a visible teacher invitation, or the student’s own application or club request. |
| `GET /admin/impersonation/published-activities` | View the school’s current published activities as the represented student or teacher. |
| `GET /admin/impersonation/clubs/{club_id}/activities/{activity_id}/revisions/{revision_id}` | Get revision content, attachment metadata, and review history for an active representative’s club. Teachers see submitted revisions. |
| `GET /admin/impersonation/clubs/{club_id}/activities/{activity_id}/revisions/{revision_id}/attachments/{attachment_id}` | Download a retained attachment for an authorized revision. |
| `POST /admin/impersonation/clubs/{club_id}/activities/{activity_id}/revisions/{revision_id}/review` | Submit an activity decision as an administrator representing the club’s active teacher representative. |

Dashboard pages accept `limit` from 1 to 50. Use each collection’s `next_cursor` as the corresponding `memberships_after`, `activities_after`, `applications_after`, `invitations_after`, or `history_after` query parameter. The publication feed uses its existing bounded list. Revision details use `attachments_after` and `reviews_after`. Directly signed-in students and teachers use the same dashboard queries through `GET /dashboard`, `GET /dashboard/clubs/{club_id}`, and the ordinary revision detail route.

School Admins representing a teacher can choose `APPROVED`, `REJECTED`, or `CHANGES_REQUESTED`. Send `decision`, `impersonation_session_id` from the current state, and `comments` in the JSON body, along with a UUID `Idempotency-Key` header. Rejection and requested changes require an explanation. A same-key retry with the same review data returns the original result while the session and year are eligible. A different revision, decision, or comment under the same key and session returns `409`. When the current session differs from the supplied identifier, the request returns `409` as well. The cookie and tab key establish authentication.

Student impersonation is read-only. For teacher impersonation, the dedicated review endpoint is the only business write. Ordinary protected routes require direct authentication. The existing heartbeat and logout continue to support impersonating tabs.

## Expiration

PostgreSQL supplies the original `started_at` and all authorization timestamps. `expires_at` is the earlier of 15 elapsed minutes from `started_at` and the parent authentication session’s deadline. Any earlier parent deadline is preserved when the parent renews. Reads, reviews, and start retries leave the cutoff in force. State responses include `started_at`, `expires_at`, `status`, `active`, and `remaining_seconds`, rounded up to whole seconds for active sessions and set to zero for terminal sessions.

Authorization denies operations checked at or after `expires_at`. If the parent is still valid, an ensuing ordinary administrator request can use the tab’s direct permissions. Requests already observed in impersonated mode retain that restriction while queued. An expired row moves to `EXPIRED` and records one `IMPERSONATION_INVALIDATED` event with the applicable timeout or parent reason. The transition commits even when the operation is denied. Request-time checks also handle revocation, loss of administrator authority, and target ineligibility. Logout ends an active session. Expiration is enforced on request without a scheduler, and a fresh start can follow expiration.

## Authorization, audit, and locks

Reviews and authorized reads recheck both accounts and the administrator’s assignment. Each successful impersonated read records the actual administrator, represented member, and impersonation session before returning the data. Reviews record the administrator as `actor_account_id`, the teacher as `represented_account_id`, the session as `impersonation_session_id`, and `IMPERSONATION` as `review_mode`. The business audit has the same identities and timestamp. Direct reviews continue to record the teacher as actor and `DIRECT` as mode. Authorized review history and the academic-year export expose `review_mode`. Impersonation reasons and session identifiers stay out of member review history and exports.

The lock sequence for an impersonated review is as follows.

1. Lock the tab’s parent authentication session for update, then the actual administrator account with `FOR NO KEY UPDATE`.
2. Lock the bound impersonation row for update, then take shared locks on the school, administrator assignment, and represented account.
3. Take a shared lock on the academic year, then update locks on the club, the target revision, and the prior published revision when approving a replacement. The club lock serializes representative membership changes. The academic-year finalizer takes an exclusive year lock before changing pending revisions.

Keep these locks until the review commits or rolls back. Lifecycle operations serialize across an administrator’s tabs on the administrator row. A competing start can inspect an older tab’s parent under that lock because logout and renewal of an active binding also serialize on it. Ordinary renewal, when applicable, finishes before authorization reacquires these locks for business access.

The admission point for a new decision is the PostgreSQL `clock_timestamp()` sample after the last business row lock. Both the seven-day academic-year cutoff and the impersonation cutoff are checked against that sample. A review admitted before both deadlines can commit after a cutoff with its locks still held. A review still waiting at the cutoff is denied. One transaction updates the decision, switches publication and starts 30-day retention for a superseded revision, and records the review and audit.

## Deployment

Apply `fe7b134d822d` with `alembic upgrade head` before serving this API. It backfills deadlines for existing tab-bound sessions using their start time and the stored parent deadline at migration time. Historical sessions and review references remain available. Use the existing session and attachment storage settings.
