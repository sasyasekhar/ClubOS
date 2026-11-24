# Academic-year history and rollover

Years progress from `ACTIVE` to `CLOSED` to `ARCHIVED`. The seven-day completion window still determines the write cutoff. Closing a year makes its business records read-only and lets the administrator create the next year. The school's closure-teacher designation continues across years.

## Archive a closed year

Generate and save a final ZIP through `POST /academic-years/{year_id}/export`. ClubOS records a durable receipt after successfully generating the ZIP from a snapshot that saw the year as `CLOSED`. The receipt proves generation on the server. The administrator verifies that the file was saved on their device.

Send `POST /academic-years/{year_id}/archive` with a UUID `Idempotency-Key` header and `{"export_saved": true}`. The endpoint returns the year, the archival administrator and timestamp, and the qualifying export reference. Retrying the same operation returns the recorded result. The lock and database transition guard enforce the forward-only lifecycle. Archived years remain exportable, and existing attachment retention continues on its original schedule.

## Carry forward selected clubs

1. Use `GET /academic-years/{target_year_id}/rollover-candidates` to list clubs in the active target year's school. An `ACTIVE` historical club status is evidence of an approved club. Candidate results include the source year and whether the club already has a copy in the target. Use `limit` from 1 to 50 and `after` from `next_cursor`.
2. Send `POST /academic-years/{target_year_id}/rollover-clubs` with a UUID `Idempotency-Key` and `source_club_ids` containing 1 to 50 distinct candidate IDs. The server validates the full batch under the school and year locks, then creates the selected records atomically. Same-key retries return the original mappings. A unique constraint prevents a second copy of the same source in the target year.
3. Each new club starts `PENDING` with its name, description, category, and source lineage. It has no representative assignments and stays unavailable for approval until the fresh assignment workflow is approved. The School Admin's selection is recorded in the audit trail. Historical records and private upload references stay with their original entities.

The ZIP includes `club_rollover_lineage.csv` when the exported year contains carried-forward clubs.

Rollover takes the school and administrator assignment locks first. It then locks all involved years in UUID order before inspecting the selected clubs under shared locks. This serializes rollover and other school-level mutations within a school, while schools can carry out independent batches. The final target status and completion-window checks occur under those locks.

## Activity feeds

| Method and path | Behavior |
| --- | --- |
| `GET /activities/published` | The school's active-year publications, using the existing list response and limit. Without an active year the list is empty. |
| `GET /academic-years/history` | A paginated list of the school's closed and archived years. |
| `GET /academic-years/{year_id}/published-activities` | A paginated list of approved, currently published revisions in a closed or archived year. |

History endpoints accept `limit` from 1 to 50 and `after` from the previous page's `next_cursor`. Active students and teachers and the assigned School Admin may browse their own school. Historical attachments use the same authorization and storage checks as current attachments. The feed hides superseded and purged content.

## Deployment

Apply `e6f1451d28aa` and `91b5d06e43c8` through `alembic upgrade head` before serving the new API. Existing historical records keep their recorded metadata. A previously closed year needs a successful new final export before archiving. Schema downgrade is safe before recording new export or rollover evidence. Once that evidence exists, the migrations refuse to remove it.

The deadline worker uses the existing database settings and still needs a verified production scheduler invocation.
