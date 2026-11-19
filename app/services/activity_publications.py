
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.school import School
from app.models.club import Club
from app.models.activity import Activity
from app.models.activity_revision import ActivityRevision
from app.models.enums import (
    AccountType,
    ClubStatus,
    ActivityRevisionStatus,
)
from app.services.activity_drafts import ActivityError


def list_published_activities(
    db: Session,
    viewer: Account,
    limit: int = 20,
):
    if (
        viewer.account_type not in (
            AccountType.STUDENT,
            AccountType.TEACHER,
            AccountType.SCHOOL_ADMIN,
        )
        or not viewer.is_active
        or viewer.school_id is None
    ):
        raise ActivityError(403, "Permission denied.")

    school = db.get(School, viewer.school_id)

    if school is None or not school.is_active:
        raise ActivityError(403, "School is inactive.")

    rows = db.execute(
        select(ActivityRevision, Club)
        .join(
            Activity,
            Activity.id == ActivityRevision.activity_id,
        )
        .join(
            Club,
            Club.id == Activity.club_id,
        )
        .where(
            Club.school_id == viewer.school_id,
            Club.status == ClubStatus.ACTIVE,
            ActivityRevision.status
            == ActivityRevisionStatus.APPROVED,
            ActivityRevision.is_published.is_(True),
        )
        .order_by(
            ActivityRevision.activity_date.desc(),
            ActivityRevision.id.desc(),
        )
        .limit(limit)
    ).all()

    return [
        {
            "activity_id": revision.activity_id,
            "revision_id": revision.id,
            "revision_number": revision.revision_number,
            "club_id": club.id,
            "club_name": club.name,
            "title": revision.title,
            "description": revision.description,
            "category": revision.category,
            "activity_date": revision.activity_date,
        }
        for revision, club in rows
    ]

