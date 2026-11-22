from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from queue import Queue
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from app.config.database import SessionLocal, engine, get_db
from app.main import app
from app.models.academic_year import AcademicYear
from app.models.account import Account
from app.models.audit_log import AuditLog
from app.models.auth_session import AuthSession
from app.models.enums import AcademicYearStatus, AccountType
from app.models.school import School
from app.models.school_admin import SchoolAdmin
from app.routes.auth import COOKIE_NAME
from app.services.sessions import IDLE_TIMEOUT, token_hash


PATH = "/academic-years"


@dataclass(frozen=True)
class YearScenario:
    school_id: UUID
    other_school_id: UUID
    admin_id: UUID
    credentials: dict[str, tuple[str, str]]

    def auth(self, actor: str = "admin") -> dict[str, str]:
        cookie, tab_key = self.credentials[actor]
        return {"Cookie": f"{COOKIE_NAME}={cookie}", "X-ClubOS-Tab": tab_key}


@pytest.fixture
def scenario():
    if engine.url.get_backend_name() != "postgresql" or engine.url.database != "clubos_test":
        pytest.fail("Use the clubos_test PostgreSQL database")

    suffix = uuid4().hex[:14]
    with SessionLocal.begin() as db:
        school = School(name="Academic Years School", school_code=f"CLB-{suffix}")
        other = School(name="Other Years School", school_code=f"CLB-{suffix}X")
        db.add_all([school, other])
        db.flush()

        identities = (
            ("admin", AccountType.SCHOOL_ADMIN, school.id),
            ("other_admin", AccountType.SCHOOL_ADMIN, other.id),
            ("unassigned", AccountType.SCHOOL_ADMIN, school.id),
            ("student", AccountType.STUDENT, school.id),
            ("teacher", AccountType.TEACHER, school.id),
            ("owner", AccountType.PLATFORM_OWNER, None),
        )
        accounts = {
            actor: Account(
                school_id=school_id,
                account_type=role,
                full_name=actor,
                username=f"{actor}_{suffix}",
                email=f"{actor}_{suffix}@example.com",
                password_hash="test-only",
            )
            for actor, role, school_id in identities
        }
        db.add_all(accounts.values())
        db.flush()
        actor_accounts = {**accounts, "second_tab": accounts["admin"]}
        credentials = {actor: (uuid4().hex, uuid4().hex) for actor in actor_accounts}
        expires_at = datetime.now(timezone.utc) + IDLE_TIMEOUT
        db.add_all(
            AuthSession(
                account_id=account.id,
                token_hash=token_hash(*credentials[actor]),
                expires_at=expires_at,
            )
            for actor, account in actor_accounts.items()
        )
        db.add_all([
            SchoolAdmin(school_id=school.id, account_id=accounts["admin"].id),
            SchoolAdmin(school_id=other.id, account_id=accounts["other_admin"].id),
        ])

        account_ids = [account.id for account in accounts.values()]
        result = YearScenario(school.id, other.id, accounts["admin"].id, credentials)

    try:
        yield result
    finally:
        school_ids = (result.school_id, result.other_school_id)
        with SessionLocal.begin() as db:
            db.execute(delete(AuditLog).where(AuditLog.actor_account_id.in_(account_ids)))
            db.execute(delete(AcademicYear).where(AcademicYear.school_id.in_(school_ids)))
            db.execute(delete(SchoolAdmin).where(SchoolAdmin.school_id.in_(school_ids)))
            db.execute(delete(Account).where(Account.id.in_(account_ids)))
            db.execute(delete(School).where(School.id.in_(school_ids)))


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def year_payload(**overrides) -> dict[str, str]:
    return {
        "name": "2026–2027",
        "start_date": "2026-06-01",
        "end_date": "2027-05-31",
        **overrides,
    }


def create(client, scenario, *, actor="admin", key=None, payload=None):
    return client.post(
        PATH,
        headers={**scenario.auth(actor), "Idempotency-Key": str(key or uuid4())},
        json=year_payload() if payload is None else payload,
    )


def insert_year(db, school_id, name, status, start_date=date(2025, 6, 1)) -> UUID:
    year = AcademicYear(
        school_id=school_id,
        name=name,
        start_date=start_date,
        end_date=date(start_date.year + 1, 5, 31),
        status=status,
    )
    db.add(year)
    db.flush()
    return year.id


def read_years(school_id: UUID) -> dict[str, AcademicYearStatus]:
    with SessionLocal() as db:
        return dict(db.execute(
            select(AcademicYear.name, AcademicYear.status)
            .where(AcademicYear.school_id == school_id)
        ).all())


def creation_audits(admin_id: UUID) -> list[AuditLog]:
    with SessionLocal() as db:
        return list(db.scalars(select(AuditLog).where(
            AuditLog.actor_account_id == admin_id,
            AuditLog.action == "ACADEMIC_YEAR_CREATED",
        )))


def test_admin_creates_and_lists_academic_year(client, scenario):
    assert client.get(PATH, headers=scenario.auth()).json() == []
    response = create(client, scenario, payload=year_payload(name="  2026–2027  "))
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"id", "name", "start_date", "end_date", "status", "created_at"}
    assert body["name"] == "2026–2027"
    assert body["start_date"] == "2026-06-01"
    assert body["end_date"] == "2027-05-31"
    assert body["status"] == "ACTIVE"
    assert datetime.fromisoformat(body["created_at"]).utcoffset() is not None

    listed = client.get(PATH, headers=scenario.auth())
    assert listed.status_code == 200
    assert listed.json() == [body]

    with SessionLocal() as db:
        year = db.get(AcademicYear, UUID(body["id"]))
        assert year is not None
        assert year.school_id == scenario.school_id
        assert (year.start_date, year.end_date) == (date(2026, 6, 1), date(2027, 5, 31))
    audit, = creation_audits(scenario.admin_id)
    assert audit.entity_type == "academic_year"
    assert audit.entity_id == body["id"]


def test_list_includes_all_school_year_statuses_in_date_order(client, scenario):
    with SessionLocal.begin() as db:
        archived_id = insert_year(
            db, scenario.school_id, "Archived", AcademicYearStatus.ARCHIVED, date(2023, 6, 1)
        )
        closed_id = insert_year(db, scenario.school_id, "Closed", AcademicYearStatus.CLOSED)
        closed_id_two = insert_year(
            db, scenario.school_id, "Also closed", AcademicYearStatus.CLOSED
        )
    active = create(client, scenario)
    assert active.status_code == 201, active.text

    result = client.get(PATH, headers=scenario.auth())
    assert result.status_code == 200
    assert [item["status"] for item in result.json()] == ["ACTIVE", "CLOSED", "CLOSED", "ARCHIVED"]
    assert [item["id"] for item in result.json()] == [
        active.json()["id"],
        *sorted((str(closed_id), str(closed_id_two))),
        str(archived_id),
    ]


def test_identical_names_and_keys_are_isolated_by_school(client, scenario):
    key = uuid4()
    first = create(client, scenario, key=key)
    other = create(client, scenario, actor="other_admin", key=key)
    assert first.status_code == other.status_code == 201, (first.text, other.text)
    assert first.json()["id"] != other.json()["id"]

    first_list = client.get(PATH, headers=scenario.auth(), params={
        "school_id": str(scenario.other_school_id),
    })
    other_list = client.get(PATH, headers=scenario.auth("other_admin"))
    assert first_list.status_code == other_list.status_code == 200
    assert first_list.json() == [first.json()]
    assert other_list.json() == [other.json()]


def test_existing_closed_year_still_reserves_name(client, scenario):
    with SessionLocal.begin() as db:
        insert_year(db, scenario.school_id, "2026–2027", AcademicYearStatus.CLOSED)

    response = create(client, scenario)
    assert response.status_code == 409, response.text
    assert read_years(scenario.school_id) == {"2026–2027": AcademicYearStatus.CLOSED}
    assert creation_audits(scenario.admin_id) == []


def test_second_active_year_conflicts_without_changing_first(client, scenario):
    first = create(client, scenario)
    assert first.status_code == 201, first.text

    second = create(client, scenario, payload=year_payload(
        name="2027–2028", start_date="2027-06-01", end_date="2028-05-31"
    ))
    assert second.status_code == 409, second.text
    assert client.get(PATH, headers=scenario.auth()).json() == [first.json()]
    assert len(creation_audits(scenario.admin_id)) == 1


def test_database_still_rejects_second_active_year(scenario):
    with SessionLocal.begin() as db:
        first = insert_year(db, scenario.school_id, "Active first", AcademicYearStatus.ACTIVE)

    with pytest.raises(IntegrityError) as conflict, SessionLocal.begin() as db:
        insert_year(db, scenario.school_id, "Active second", AcademicYearStatus.ACTIVE)

    assert conflict.value.orig.diag.constraint_name == "uq_one_active_academic_year_per_school"
    with SessionLocal() as db:
        assert db.get(AcademicYear, first).status is AcademicYearStatus.ACTIVE


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"name": " \t "},
        {"name": "a" * 51},
        {"name": "2026\x00–2027"},
        {"start_date": "2026-02-30"},
        {"start_date": "2026-06-01T00:00:00"},
        {"start_date": 1780272000},
        {"end_date": "2026-06-01"},
        {"end_date": "2026-05-31"},
        {"status": "CLOSED"},
        {"status": "ARCHIVED"},
    ],
)
def test_create_rejects_invalid_payload(client, scenario, overrides):
    response = create(client, scenario, payload=year_payload(**overrides))
    assert response.status_code == 422, response.text
    assert read_years(scenario.school_id) == {}
    assert creation_audits(scenario.admin_id) == []


def test_create_rejects_client_supplied_school(client, scenario):
    response = create(client, scenario, payload=year_payload(
        school_id=str(scenario.other_school_id),
    ))
    assert response.status_code == 422, response.text
    assert read_years(scenario.school_id) == {}
    assert read_years(scenario.other_school_id) == {}


@pytest.mark.parametrize("key", [None, "malformed"])
def test_create_requires_uuid_idempotency_key(client, scenario, key):
    headers = scenario.auth()
    if key is not None:
        headers["Idempotency-Key"] = key
    response = client.post(PATH, headers=headers, json=year_payload())
    assert response.status_code == 422, response.text
    assert read_years(scenario.school_id) == {}


def test_identical_retry_replays_without_duplicate_year_or_audit(client, scenario):
    key = uuid4()
    first = create(client, scenario, key=key)
    second = create(client, scenario, actor="second_tab", key=key)
    assert first.status_code == second.status_code == 201, (first.text, second.text)
    assert first.json() == second.json()
    assert read_years(scenario.school_id) == {"2026–2027": AcademicYearStatus.ACTIVE}
    assert len(creation_audits(scenario.admin_id)) == 1


def test_same_key_with_changed_payload_conflicts(client, scenario):
    key = uuid4()
    original = create(client, scenario, key=key)
    assert original.status_code == 201, original.text

    for changed in (
        {"name": "Changed"},
        {"start_date": "2026-06-02"},
        {"end_date": "2027-05-30"},
    ):
        result = create(client, scenario, key=key, payload=year_payload(**changed))
        assert result.status_code == 409, result.text
    assert client.get(PATH, headers=scenario.auth()).json() == [original.json()]
    assert len(creation_audits(scenario.admin_id)) == 1


@pytest.mark.parametrize("actor", ["student", "teacher", "owner", "unassigned"])
def test_admin_assignment_required_for_creation_and_listing(client, scenario, actor):
    response = create(client, scenario, actor=actor)
    assert response.status_code == 403, response.text
    response = client.get(PATH, headers=scenario.auth(actor))
    assert response.status_code == 403, response.text
    assert read_years(scenario.school_id) == {}


def test_session_cookie_and_tab_key_required(client, scenario):
    headers = scenario.auth()
    for name, value in headers.items():
        response = client.post(
            PATH,
            headers={name: value, "Idempotency-Key": str(uuid4())},
            json=year_payload(),
        )
        assert response.status_code == 401, response.text
        response = client.get(PATH, headers={name: value})
        assert response.status_code == 401, response.text


@pytest.mark.parametrize(
    "same_key,same_payload,expected",
    [
        (True, True, [201, 201]),
        (True, False, [201, 409]),
        (False, True, [201, 409]),
        (False, False, [201, 409]),
    ],
)
def test_concurrent_creates_preserve_one_year_and_one_audit(
    monkeypatch, scenario, same_key, same_payload, expected
):
    ready = Barrier(2, timeout=10)
    backend_pids = Queue()

    def synchronized_db():
        with SessionLocal() as db:
            db.execute(text("SET LOCAL statement_timeout = '10s'"))
            backend_pids.put(db.scalar(select(func.pg_backend_pid())))
            ready.wait()
            yield db

    def request(actor, key, payload):
        with TestClient(app) as client:
            return create(client, scenario, actor=actor, key=key, payload=payload)

    monkeypatch.setitem(app.dependency_overrides, get_db, synchronized_db)
    first_key = uuid4()
    second_key = first_key if same_key else uuid4()
    first_payload = year_payload()
    second_payload = first_payload if same_payload else year_payload(
        name="2027–2028", start_date="2027-06-01", end_date="2028-05-31"
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(request, "admin", first_key, first_payload)
        second = executor.submit(request, "second_tab", second_key, second_payload)
        responses = (first.result(timeout=20), second.result(timeout=20))

    assert backend_pids.get_nowait() != backend_pids.get_nowait()
    assert sorted(response.status_code for response in responses) == expected, [
        response.text for response in responses
    ]
    created = [response.json() for response in responses if response.status_code == 201]
    for body in created:
        assert body == created[0]
    assert read_years(scenario.school_id) == {created[0]["name"]: AcademicYearStatus.ACTIVE}
    audit, = creation_audits(scenario.admin_id)
    assert audit.entity_id == created[0]["id"]
