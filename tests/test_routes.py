from app.main import app


def test_school_registration_route():
    paths = app.openapi()["paths"]

    assert "/auth/register-school" in paths
    assert "post" in paths["/auth/register-school"]
    