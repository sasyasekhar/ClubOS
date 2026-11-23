from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text, update

from app.config.database import SessionLocal
from app.models.academic_year_closure_request import AcademicYearClosureRequest
from app.models.audit_log import AuditLog
from app.models.club_membership import ClubMembership
from app.models.enums import ActivityRevisionStatus, ClubRole, ClubStatus

from test_academic_year_closure_workflow import add_activity, client, designated, scenario


def alerts(client, scenario, actor="teacher", year="active", **params):
    return client.get(
        f"/academic-years/{scenario.years[year]}/pending-review-alerts",
        headers=scenario.auth(actor),
        params=params,
    )


def start_window(client, scenario):
    response = scenario.request(client)
    assert response.status_code == 201, response.text
    return response.json()


def audit_count(scenario):
    with SessionLocal() as db:
        return db.scalar(select(func.count(AuditLog.id)).where(
            AuditLog.actor_account_id.in_(scenario.accounts.values()),
        ))


def test_pending_alerts_are_live_and_polling_does_not_create_events(client, scenario, designated):
    with SessionLocal.begin() as db:
        club, activity, (pending,) = add_activity(db, scenario, ActivityRevisionStatus.PENDING)
        new_club, new_activity, (draft,) = add_activity(db, scenario, ActivityRevisionStatus.DRAFT)
    assert alerts(client, scenario).json() == {"items": [], "next_cursor": None}

    window = start_window(client, scenario)
    initial = alerts(client, scenario)
    assert initial.status_code == 200, initial.text
    assert initial.headers["Cache-Control"] == "private, no-store"
    item, = initial.json()["items"]
    assert set(item) == {
        "academic_year_id", "club_id", "club_name", "activity_id", "revision_id", "message",
        "completion_deadline_at", "created_at", "needs_attention",
    }
    assert {key: value for key, value in item.items() if key not in {"completion_deadline_at", "created_at"}} == {
        "academic_year_id": str(scenario.years["active"]),
        "club_id": str(club),
        "club_name": "Robotics",
        "activity_id": str(activity),
        "revision_id": str(pending),
        "message": "Robotics has an activity awaiting review.",
        "needs_attention": False,
    }
    assert datetime.fromisoformat(item["created_at"]) == datetime.fromisoformat(window["requested_at"])
    assert datetime.fromisoformat(item["completion_deadline_at"]) == datetime.fromisoformat(window["completion_deadline_at"])
    assert datetime.fromisoformat(item["completion_deadline_at"]).utcoffset() == timedelta(0)
    before_polling = audit_count(scenario)
    assert alerts(client, scenario).json() == initial.json()
    assert audit_count(scenario) == before_polling

    submit = client.post(
        f"/clubs/{new_club}/activities/{new_activity}/revisions/{draft}/submit",
        headers=scenario.auth("student"),
    )
    assert submit.status_code == 200, submit.text
    rows = alerts(client, scenario).json()["items"]
    assert {row["revision_id"] for row in rows} == {str(pending), str(draft)}
    assert {datetime.fromisoformat(row["completion_deadline_at"]) for row in rows} == {
        datetime.fromisoformat(window["completion_deadline_at"]),
    }

    review = client.post(
        f"/clubs/{club}/activities/{activity}/revisions/{pending}/review",
        headers={**scenario.auth("teacher"), "Idempotency-Key": str(uuid4())},
        json={"decision": "APPROVED"},
    )
    assert review.status_code == 201, review.text
    assert [row["revision_id"] for row in alerts(client, scenario).json()["items"]] == [str(draft)]
    assert alerts(client, scenario, actor="replacement").json()["items"] == []


def test_pending_alerts_have_stable_cursor_pages(client, scenario, designated):
    with SessionLocal.begin() as db:
        _, _, revisions = add_activity(db, scenario, *([ActivityRevisionStatus.PENDING] * 3))
    start_window(client, scenario)

    first = alerts(client, scenario, limit=2).json()
    assert [row["revision_id"] for row in first["items"]] == [str(id_) for id_ in sorted(revisions)[:2]]
    assert first["next_cursor"] == str(sorted(revisions)[1])
    second = alerts(client, scenario, limit=2, after=first["next_cursor"]).json()
    assert [row["revision_id"] for row in second["items"]] == [str(max(revisions))]
    assert second["next_cursor"] is None
    assert alerts(client, scenario, limit=51).status_code == 422
    assert alerts(client, scenario, after="not-a-uuid").status_code == 422


def test_admin_can_find_review_items_that_need_attention(client, scenario, designated):
    with SessionLocal.begin() as db:
        _, _, (assigned,) = add_activity(db, scenario, ActivityRevisionStatus.PENDING)
        no_teacher_club, _, (unassigned,) = add_activity(db, scenario, ActivityRevisionStatus.PENDING)
        inactive_teacher_club, _, (inactive_teacher,) = add_activity(db, scenario, ActivityRevisionStatus.PENDING)
        foreign_teacher_club, _, (foreign_teacher,) = add_activity(db, scenario, ActivityRevisionStatus.PENDING)
        _, _, (inactive_club,) = add_activity(
            db, scenario, ActivityRevisionStatus.PENDING, club_status=ClubStatus.ARCHIVED,
        )
        db.execute(delete(ClubMembership).where(
            ClubMembership.club_id == no_teacher_club,
            ClubMembership.role == ClubRole.TEACHER_REP,
        ))
        for club, actor in ((inactive_teacher_club, "inactive"), (foreign_teacher_club, "other_teacher")):
            db.execute(update(ClubMembership).where(
                ClubMembership.club_id == club,
                ClubMembership.role == ClubRole.TEACHER_REP,
            ).values(account_id=scenario.accounts[actor]))
    start_window(client, scenario)

    teacher = alerts(client, scenario)
    assert [row["revision_id"] for row in teacher.json()["items"]] == [str(assigned)]
    assert alerts(client, scenario, attention_only=True).status_code == 403

    admin = alerts(client, scenario, actor="admin")
    assert admin.status_code == 200, admin.text
    assert {row["revision_id"]: row["needs_attention"] for row in admin.json()["items"]} == {
        str(assigned): False,
        str(unassigned): True,
        str(inactive_teacher): True,
        str(foreign_teacher): True,
        str(inactive_club): True,
    }
    attention = alerts(client, scenario, actor="admin", attention_only=True).json()["items"]
    assert {row["revision_id"] for row in attention} == {
        str(unassigned), str(inactive_teacher), str(foreign_teacher), str(inactive_club),
    }


@pytest.mark.parametrize(
    "actor, status",
    [(None, 401), ("student", 403), ("owner", 403), ("unassigned", 403), ("other_admin", 404), ("other_teacher", 404)],
)
def test_pending_alerts_reject_unauthorized_access(client, scenario, actor, status):
    assert alerts(client, scenario, actor=actor).status_code == status


def test_expired_pending_items_remain_visible_to_admin_until_resolved(client, scenario, designated):
    with SessionLocal.begin() as db:
        _, _, (revision,) = add_activity(db, scenario, ActivityRevisionStatus.PENDING)
    start_window(client, scenario)
    with SessionLocal.begin() as db:
        db.execute(update(AcademicYearClosureRequest).where(
            AcademicYearClosureRequest.academic_year_id == scenario.years["active"],
        ).values(
            requested_at=func.statement_timestamp() - text("interval '169 hours'"),
            completion_deadline_at=func.statement_timestamp() - text("interval '1 hour'"),
        ))

    assert alerts(client, scenario).json()["items"] == []
    assert [row["revision_id"] for row in alerts(client, scenario, actor="admin").json()["items"]] == [str(revision)]
