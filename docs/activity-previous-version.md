# Recover a previous activity publication

The current active student representative can preview the publication that was most recently superseded for an activity in their club. ClubOS uses the superseded timestamp, then the revision number to break ties. Preview and student downloads expire at exactly 30 elapsed days from that timestamp. The original historical row and its timestamp determine eligibility even after its content has been purged.

| Method and path | Behavior |
| --- | --- |
| `GET /clubs/{club_id}/activities/{activity_id}/previous-version` | Returns the single previous publication, its content, the current publication ID, the recorded approval time if available, the superseded time, and the recovery deadline. Attachment entries contain IDs and safe metadata. `unavailable_attachment_count` counts files whose metadata or scan evidence prevents a copy. |
| `POST /clubs/{club_id}/activities/{activity_id}/restore-previous-version` | Requires a UUID `Idempotency-Key` header and JSON containing `expected_revision_id` from the preview. Returns the new draft identity, revision number, and status. |

The API uses the existing session cookie and `X-ClubOS-Tab`. An eligible previous attachment can be opened through its ordinary authenticated download URL. Object availability and integrity are verified when reading the file. The ordinary student dashboard omits superseded revisions. Teacher and School Admin history follows their existing access rules.

Restore creates a fresh unpublished draft. It uses the normal edit, submit, and teacher review flow. The current publication stays visible until approval switches publication atomically. A stale expected ID or unfinished revision returns `409`. Missing previous history returns `404`. Expired or purged previous content returns `410`. A file with unverified metadata blocks restoration, and a missing or corrupt object returns `503`.

The restore checks school and membership under the academic-year and club locks, then takes a shared source-revision lock. It verifies each source object against its size, SHA-256, and recorded clean scan and writes an independent object under the new draft. It samples PostgreSQL time after the file copies and database writes to enforce both deadlines. Revision metadata, the audit event, and the durable operation receipt commit together. If a commit acknowledgement is lost, orphan reconciliation checks the destination revision before deleting its objects. A retry with the same key and expected ID returns the recorded draft.
