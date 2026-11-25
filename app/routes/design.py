"""Loopback-only previews with synthetic data and no database dependencies."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.templating import Jinja2Templates

from app.config.settings import settings


def require_local_development(request: Request) -> None:
    if (
        settings.APP_ENV != "development"
        or request.client is None
        or request.client.host not in {"127.0.0.1", "::1"}
        or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
    ):
        raise HTTPException(404, "Not found")


router = APIRouter(
    prefix="/dev/design", include_in_schema=False,
    dependencies=[Depends(require_local_development)],
)
templates = Jinja2Templates(directory="app/templates")
PRIVATE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
PREVIEW_HEADERS = {
    **PRIVATE,
    "Content-Security-Policy": (
        "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
}

ACCOUNTS = (
    {"id": "demo-1", "name": "Alex Morgan", "username": "alex.m.teacher", "role": "Teacher"},
    {"id": "demo-2", "name": "Alex Morgan", "username": "alex.m.student", "role": "Student"},
    {"id": "demo-3", "name": "Alex Rivera", "username": "alex.r.teacher", "role": "Teacher"},
    {"id": "demo-4", "name": "Casey Shah", "username": "casey.s.student", "role": "Student"},
)

ACTIVITIES = (
    ("Campus bird count", "Nature Club", "Awaiting review", "warning"),
    ("Radio workshop", "Media Club", "Changes requested", "danger"),
    ("Ceramics open studio", "Art Club", "Approved", "success"),
    ("Spring chess meet", "Chess Club", "Awaiting review", "warning"),
    ("Library reading circle", "Literary Club", "Approved", "success"),
    ("Robotics build day", "Science Club", "Draft", "neutral"),
)

PROFILE = {"name": "Morgan Bell", "role": "School Administrator", "school": "Cedar Grove School", "initials": "MB"}
AUTH_FEEDBACK = (
    ("Invalid credentials", "Your username or password is incorrect. Please check your details and try again."),
    ("Network failure", "We couldn’t reach ClubOS. Check your connection and try again."),
    ("Too many attempts", "Too many sign-in attempts. Wait a few minutes before trying again."),
    ("Expired session", "Your session has expired. Sign in again to continue."),
    ("Service unavailable", "ClubOS is temporarily unavailable. Please try again shortly."),
)


@router.get("")
def design_showcase(request: Request):
    return templates.TemplateResponse(
        request, "pages/design.html",
        {"profile": PROFILE, "activities": ACTIVITIES}, headers=PREVIEW_HEADERS,
    )


@router.get("/auth")
def design_auth_preview(request: Request):
    return templates.TemplateResponse(
        request, "pages/design-auth.html", {"feedback": AUTH_FEEDBACK}, headers=PREVIEW_HEADERS,
    )


@router.get("/accounts")
def design_account_search(
    response: Response, q: str = Query("", max_length=100), after: int = Query(0, ge=0, le=len(ACCOUNTS)),
):
    # These fixtures represent only one fictional school. The live route applies server-side eligibility.
    response.headers.update(PRIVATE)
    query = q.strip().casefold()
    matches = [account for account in ACCOUNTS if query and query in account["name"].casefold()]
    page_size = 2
    end = after + page_size
    return {"items": matches[after:end], "next_cursor": end if end < len(matches) else None}
