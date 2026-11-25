from fastapi.testclient import TestClient
import pytest

from app.config.settings import settings
from app.main import app
from app.routes import design


def local_client(host: str = "127.0.0.1", base_url: str = "http://localhost"):
    return TestClient(app, client=(host, 12345), base_url=base_url)


@pytest.mark.parametrize(
    ("environment", "peer", "base_url"),
    [
        ("production", "127.0.0.1", "http://localhost"),
        ("test", "127.0.0.1", "http://localhost"),
        ("development", "192.0.2.10", "http://localhost"),
        ("development", "127.0.0.1", "https://example.test"),
    ],
)
def test_preview_is_unavailable_outside_local_development(monkeypatch, environment, peer, base_url):
    monkeypatch.setattr(settings, "APP_ENV", environment)
    client = local_client(peer, base_url)
    for path in ("/dev/design", "/dev/design/auth", "/dev/design/accounts?q=alex"):
        assert client.get(path).status_code == 404


def test_preview_renders_shared_layout_and_escapes_profile(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setitem(design.PROFILE, "name", '<script>alert("synthetic")</script>')
    client = local_client()
    workspace = client.get("/dev/design")
    auth = client.get("/dev/design/auth")

    assert workspace.status_code == auth.status_code == 200
    assert "/static/css/clubos.css" in workspace.text and "/static/css/clubos.css" in auth.text
    assert 'id="main-content"' in workspace.text and 'id="main-content"' in auth.text
    assert "&lt;script&gt;" in workspace.text
    assert '<script>alert("synthetic")</script>' not in workspace.text
    assert "form-action 'none'" in auth.headers["Content-Security-Policy"]
    assert "/dev/design" not in client.get("/openapi.json").json()["paths"]


def test_synthetic_search_returns_a_bounded_page(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "development")
    client = local_client()
    first = client.get("/dev/design/accounts?q=alex")
    assert first.headers["Cache-Control"] == "no-store"
    page = first.json()
    assert len(page["items"]) == 2
    second = client.get("/dev/design/accounts", params={"q": "alex", "after": page["next_cursor"]}).json()
    assert [item["name"] for item in second["items"]] == ["Alex Rivera"]
    assert second["next_cursor"] is None
    assert client.get("/dev/design/accounts", params={"q": ""}).json()["items"] == []
