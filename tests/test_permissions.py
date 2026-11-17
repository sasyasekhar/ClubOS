
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, Depends, HTTPException
from fastapi.testclient import TestClient

from app.models.enums import AccountType
from app.routes.auth import require_account
from app.security.permissions import (
    require_roles,
    ensure_same_school,
)


@pytest.mark.parametrize(
    "role,expected",
    [
        (AccountType.STUDENT, 200),
        (AccountType.TEACHER, 403),
        (AccountType.SCHOOL_ADMIN, 403),
        (AccountType.PLATFORM_OWNER, 403),
    ],
)
def test_student_route_permissions(role, expected):
    app = FastAPI()

    @app.get("/student-only")
    def student_only(
        account=Depends(require_roles(AccountType.STUDENT)),
    ):
        return {"role": account.account_type.value}

    app.dependency_overrides[require_account] = lambda: (
        SimpleNamespace(account_type=role)
    )

    with TestClient(app) as client:
        response = client.get("/student-only")

    assert response.status_code == expected


def test_same_school_allowed():
    school_id = uuid4()
    account = SimpleNamespace(school_id=school_id)

    ensure_same_school(account, school_id)


def test_cross_school_denied():
    account = SimpleNamespace(school_id=uuid4())

    with pytest.raises(HTTPException) as error:
        ensure_same_school(account, uuid4())

    assert error.value.status_code == 404


def test_platform_owner_cannot_access_school_resources():
    account = SimpleNamespace(
        school_id=None,
        account_type=AccountType.PLATFORM_OWNER,
    )

    with pytest.raises(HTTPException) as error:
        ensure_same_school(account, uuid4())

    assert error.value.status_code == 404

