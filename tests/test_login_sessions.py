
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.config.database import engine, get_db
from app.models.auth_session import AuthSession
from app.schemas.auth import SchoolRegistration
from app.services.school_registration import register_school


@pytest.fixture
def env():
    if engine.url.database != "clubos_test":
        pytest.fail("Use clubos_test")

    with engine.connect() as connection:
        transaction = connection.begin()
        db = Session(
            bind=connection,
            join_transaction_mode="create_savepoint",
        )

        suffix = uuid.uuid4().hex[:12]
        password = "SecureAdminPassword123!"

        school = register_school(db, SchoolRegistration(
            school_name=f"Session School {suffix}",
            admin_full_name="Test Admin",
            username=f"admin_{suffix}",
            email=f"admin_{suffix}@example.com",
            password=password,
        ))

        def override_db():
            yield db

        app.dependency_overrides[get_db] = override_db

        try:
            with TestClient(app) as client:
                yield client, db, f"admin_{suffix}", password
        finally:
            app.dependency_overrides.clear()
            db.close()
            transaction.rollback()


def login(env):
    client, db, username, password = env
    response = client.post("/auth/login", json={
        "identifier": username,
        "password": password,
    })
    assert response.status_code == 200
    return response.json()["tab_key"]


def test_login_and_tab_access(env):
    client, db, username, password = env
    tab_key = login(env)

    cookie = next(
        cookie for cookie in client.cookies.jar
        if cookie.name == "clubos_session"
    )
    assert cookie.has_nonstandard_attr("HttpOnly")

    headers = {"X-ClubOS-Tab": tab_key}

    assert client.get("/auth/me", headers=headers).status_code == 200

    # A new tab without the sessionStorage key is denied.
    assert client.get("/auth/me").status_code == 401

    assert client.get(
        "/auth/me",
        headers={"X-ClubOS-Tab": "invalid"},
    ).status_code == 401


def test_logout_revokes_session(env):
    client, _, _, _ = env
    tab_key = login(env)
    headers = {"X-ClubOS-Tab": tab_key}

    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_heartbeat_renews_active_session(env):
    client, db, _, _ = env
    tab_key = login(env)
    headers = {"X-ClubOS-Tab": tab_key}

    session = db.scalar(select(AuthSession))

    session.expires_at = datetime.now(timezone.utc) + timedelta(seconds=5)
    db.flush()

    assert client.post("/auth/heartbeat", headers=headers).status_code == 200

    db.refresh(session)
    assert session.expires_at > datetime.now(timezone.utc) + timedelta(hours=11)


def test_expired_session_rejected(env):
    client, db, _, _ = env
    tab_key = login(env)

    session = db.scalar(select(AuthSession))
    session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.flush()

    assert client.get(
        "/auth/me",
        headers={"X-ClubOS-Tab": tab_key},
    ).status_code == 401


def test_wrong_password(env):
    client, _, username, _ = env

    response = client.post("/auth/login", json={
        "identifier": username,
        "password": "IncorrectPassword123!",
    })

    assert response.status_code == 401

