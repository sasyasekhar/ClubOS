from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.config.database import SessionLocal
from app.config.settings import settings
from app.main import app
from app.models.account import Account
from app.models.login_history import LoginHistory
from app.security.password import hash_password
from app.security.requests import MAX_REQUEST_BODY
from app.services.login_throttling import IDENTIFIER_FAILURE_LIMIT
from app.services.sessions import database_now
from test_academic_year_closure_workflow import client, scenario
from test_admin_impersonation import ControlledClock


def test_cross_origin_does_not_revoke_cookie_authenticated_session(client, scenario):
    headers = scenario.auth("admin")
    refused = client.post("/auth/logout", headers={**headers, "Origin": "https://other.example.test"})
    assert refused.status_code == 403
    assert client.get("/auth/me", headers=headers).status_code == 200
    assert client.post("/auth/logout", headers={**headers, "Origin": "http://testserver"}).status_code == 204
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_cross_site_login_is_denied_even_without_an_origin(client):
    response = client.post("/auth/login", headers={"Sec-Fetch-Site": "cross-site"},
                           json={"identifier": "sample", "password": "fixture"})
    assert response.status_code == 403 and "set-cookie" not in response.headers


@pytest.mark.parametrize("streamed", [False, True])
def test_large_json_request_is_rejected_for_fixed_and_streamed_bodies(client, streamed):
    def chunks():
        for _ in range(3):
            yield b" " * (MAX_REQUEST_BODY // 2)

    body = chunks() if streamed else b" " * (MAX_REQUEST_BODY + 1)
    response = client.post("/auth/login", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_invalid_password_does_not_echo_the_submitted_value(client):
    password = "PrivateFixtureValue" * 20
    response = client.post("/auth/login", json={"identifier": "sample", "password": password})
    assert response.status_code == 422 and password not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert all("input" not in item and "ctx" not in item for item in response.json()["detail"])


def test_login_throttle_is_shared_across_clients_and_expires(monkeypatch):
    identifier = f"fixture_{uuid4().hex}"
    identifier_key = sha256(f"clubos:login:identifier:{identifier}".encode()).hexdigest()
    with SessionLocal() as db:
        clock = ControlledClock(database_now(db))
    monkeypatch.setattr(func, "clock_timestamp", clock)
    try:
        with TestClient(app, client=("192.0.2.11", 12345)) as first, TestClient(app, client=("192.0.2.12", 12345)) as second:
            payload = {"identifier": identifier, "password": "fixture"}
            for _ in range(IDENTIFIER_FAILURE_LIMIT):
                result = first.post("/auth/login", json=payload)
                assert result.status_code == 401 and result.json() == {"detail": "Invalid credentials"}
            blocked = second.post("/auth/login", json={**payload, "identifier": identifier.upper()})
            assert blocked.status_code == 429 and blocked.headers["Retry-After"] == "300"
            clock.now += timedelta(minutes=5)
            assert second.post("/auth/login", json=payload).status_code == 401
        with SessionLocal() as db:
            failures = db.scalars(select(LoginHistory).where(LoginHistory.identifier_key == identifier_key)).all()
            assert len(failures) == IDENTIFIER_FAILURE_LIMIT + 1
            assert all(row.attempted_identifier is None for row in failures)
    finally:
        with SessionLocal.begin() as db:
            db.execute(delete(LoginHistory).where(LoginHistory.identifier_key == identifier_key))


def test_invalid_stored_hash_returns_the_same_login_error(client, scenario):
    with SessionLocal() as db:
        username = db.get(Account, scenario.accounts["student"]).username
    failure = client.post("/auth/login", json={"identifier": username, "password": "FixturePassword123!"})
    assert failure.status_code == 401 and failure.json() == {"detail": "Invalid credentials"}


def test_production_login_cookie_is_secure_even_when_development_flag_is_false(client, scenario, monkeypatch):
    with SessionLocal.begin() as db:
        account = db.get(Account, scenario.accounts["student"])
        account.password_hash = hash_password("FixturePassword123!")
        identifier = account.username
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    response = client.post("/auth/login", json={"identifier": identifier, "password": "FixturePassword123!"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
