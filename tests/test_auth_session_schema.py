
import pytest
from sqlalchemy import inspect

from app.config.database import engine


def test_auth_session_table_exists():
    if engine.url.database != "clubos_test":
        pytest.fail("Tests require clubos_test")

    inspector = inspect(engine)

    assert inspector.has_table("auth_sessions")

    columns = {
        column["name"]
        for column in inspector.get_columns("auth_sessions")
    }

    assert {
        "id",
        "account_id",
        "token_hash",
        "created_at",
        "expires_at",
        "revoked_at",
    }.issubset(columns)
