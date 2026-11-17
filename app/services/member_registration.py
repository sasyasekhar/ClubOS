
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.school import School
from app.schemas.auth import MemberRegistration
from app.security.password import hash_password
from app.services.school_registration import RegistrationConflict


class SchoolNotAvailable(Exception):
    pass


def register_member(db: Session, data: MemberRegistration):
    school_code = data.school_code.strip().upper()

    school = db.scalar(
        select(School).where(
            School.school_code == school_code,
            School.is_active.is_(True),
        )
    )

    if school is None:
        raise SchoolNotAvailable()

    try:
        account = Account(
            school_id=school.id,
            account_type=data.account_type,
            full_name=data.full_name.strip(),
            username=data.username.strip(),
            email=str(data.email).lower(),
            password_hash=hash_password(data.password),
        )

        db.add(account)
        db.flush()

        result = {
            "account_id": account.id,
            "school_id": account.school_id,
            "account_type": account.account_type,
        }

        db.commit()
        return result

    except IntegrityError as exc:
        db.rollback()
        raise RegistrationConflict() from exc

    except Exception:
        db.rollback()
        raise
