# Academic-year history and rollover

Years progress from `ACTIVE` to `CLOSED` to `ARCHIVED`. The seven-day completion window still determines the write cutoff. Closing a year makes its business records read-only and lets the administrator create the next year. The school's closure-teacher designation continues across years.

## Archive a closed year

Generate and save a final ZIP through `POST /academic-years/{year_id}/export`. ClubOS records a durable receipt after successfully generating the ZIP from a snapshot that saw the year as `CLOSED`. The receipt proves generation on the server. The administrator verifies that the file was saved on their device.

`activity_reviews.csv` includes `review_mode` with `DIRECT` for teacher reviews and `IMPERSONATION` for reviews recorded through an authorized impersonation.

Send `POST /academic-years/{year_id}/archive` with a UUID `Idempotency-Key` header and `{"export_saved": true}`. The endpoint returns the year, the archival administrator and timestamp, and the qualifying export reference. Retrying the same operation returns the recorded result. The lock and database transition guard enforce the forward-only lifecycle. Archived years remain exportable, and existing attachment retention continues on its original schedule.

## Carry forward selected clubs

1. Use `GET /academic-years/{target_year_id}/rollover-candidates` to list clubs in the active target year's school. An `ACTIVE` historical club status is evidence of an approved club. Candidate results include the source year and whether the club already has a copy in the target. Use `limit` from 1 to 50 and `after` from `next_cursor`.
2. Send `POST /academic-years/{target_year_id}/rollover-clubs` with a UUID `Idempotency-Key` and `source_club_ids` containing 1 to 50 distinct candidate IDs. The server validates the full batch under the school and year locks, then creates the selected records atomically. Same-key retries return the original mappings. A unique constraint prevents a second copy of the same source in the target year.
3. Each new club starts `PENDING` with its name, description, category, and source lineage. It has no representative assignments. The School Admin's rollover selection is recorded in the audit trail. Historical records and private upload references stay with their original entities.

The ZIP includes `club_rollover_lineage.csv` when the exported year contains carried-forward clubs.

Rollover takes the school and administrator assignment locks first. It then locks all involved years in UUID order before inspecting the selected clubs under shared locks. This serializes rollover and other school-level mutations within a school, while schools can carry out independent batches. The final target status and completion-window checks occur under those locks.

## Assign representatives to a carried-forward club

1. A student uses `GET /clubs/available-representative-positions` to find open positions in the current year and `POST /clubs/{club_id}/representative-applications` to apply. `GET /clubs/my-representative-applications` shows only that student's applications and decisions. Students may apply to multiple clubs.
2. The assigned School Admin uses `GET /clubs/{club_id}/representative-applications` to list applicants and `POST /clubs/{club_id}/representative-applications/{application_id}/review` to submit a `SELECTED`, `REJECTED`, or `CHANGES_REQUESTED` decision. Rejection and requested changes require `comments`. Selection closes the position to new applications and competing selections. The selected application records the administrator and the decision date. Other applications keep their recorded state until reviewed.
3. The selected student uses `GET /clubs/{club_id}/eligible-teachers` to choose a same-school teacher and `POST /clubs/{club_id}/select-teacher` with `teacher_account_id` to invite them. The teacher gets the `membership_id` from `GET /clubs/teacher-invitations` and sends it with the `ACCEPT` or `DECLINE` decision to `POST /clubs/{club_id}/teacher-invitation`. After a decline, the selected student uses `POST /clubs/{club_id}/replace-teacher` to send a new invitation.
4. After teacher acceptance, the School Admin approves the club through the existing `POST /clubs/{club_id}/reviews` endpoint. Approval activates the club and creates the selected student's active membership in the same transaction. The teacher and student can manage activities once the club is active. The carried-forward record keeps its original source reference and its empty creator field.

The application, application review, teacher selection, replacement, and approval writes accept a UUID `Idempotency-Key`. The list endpoints use `limit` from 1 to 50 and `after` from `next_cursor`. Setup writes follow the same year locks and completion deadline as ordinary club writes. Membership history is included in academic-year exports. Private application records are visible through the application endpoints to the assigned administrator and to each applicant for their own applications.

## Activity feeds

| Method and path | Behavior |
| --- | --- |
| `GET /activities/published` | The school's active-year publications, using the existing list response and limit. Without an active year the list is empty. |
| `GET /academic-years/history` | A paginated list of the school's closed and archived years. |
| `GET /academic-years/{year_id}/published-activities` | A paginated list of approved, currently published revisions in a closed or archived year. |

History endpoints accept `limit` from 1 to 50 and `after` from the previous page's `next_cursor`. Active students and teachers and the assigned School Admin may browse their own school. Historical attachments use the same authorization and storage checks as current attachments. The feed hides superseded and purged content.

## Deployment

Apply `e6f1451d28aa`, `91b5d06e43c8`, and `57c3df68ad09` through `alembic upgrade head` before serving the new API. Existing historical records keep their recorded metadata. A previously closed year needs a successful new final export before archiving. Each migration supports downgrade before recording the corresponding export, rollover, or application data. Once that data exists, the migration refuses to remove it.

The deadline worker uses the existing database settings and still needs a verified production scheduler invocation.
