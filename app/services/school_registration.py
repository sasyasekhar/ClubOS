
import secrets
import string

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.school import School
from app.models.account import Account
from app.models.school_admin import SchoolAdmin
from app.models.enums import AccountType
from app.schemas.auth import SchoolRegistration
from app.security.password import hash_password


ALPHABET = string.ascii_uppercase + string.digits


class RegistrationConflict(Exception):
    pass


def generate_school_code() -> str:
    return "CLB-" + "".join(
        secrets.choice(ALPHABET) for _ in range(12)
    )


def register_school(db: Session, data: SchoolRegistration):
    try:
        school = School(
            name=data.school_name.strip(),
            school_code=generate_school_code(),
        )
        db.add(school)
        db.flush()

        account = Account(
            school_id=school.id,
            account_type=AccountType.SCHOOL_ADMIN,
            full_name=data.admin_full_name.strip(),
            username=data.username.strip(),
            email=str(data.email).lower(),
            password_hash=hash_password(data.password),
        )
        db.add(account)
        db.flush()

        admin = SchoolAdmin(
            school_id=school.id,
            account_id=account.id,
        )
        db.add(admin)
        db.flush()

        result = {
            "school_id": school.id,
            "school_code": school.school_code,
            "admin_account_id": account.id,
        }

        db.commit()
        return result

    except IntegrityError as exc:
        db.rollback()
        raise RegistrationConflict() from exc

    except Exception:
        db.rollback()
        raise
