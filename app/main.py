from contextlib import asynccontextmanager
import logging
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from app.config.settings import settings
from app.routes import (
    academic_year_history, academic_years, activities, auth, closure_alerts, clubs, dashboard, impersonation, publications,
    representative_applications,
)
from app.security.requests import RequestSecurityMiddleware
from app.services.storage import get_attachment_storage


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_attachment_storage()
    if settings.APP_ENV == "production":
        origin = urlsplit(settings.PUBLIC_ORIGIN or "")
        if origin.scheme != "https" or not origin.hostname or origin.username is not None or origin.path or origin.query or origin.fragment:
            raise RuntimeError("Set PUBLIC_ORIGIN to the deployment's exact HTTPS origin.")
    yield

app = FastAPI(
    title="ClubOS",
    description="A multi-school club management application",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(RequestSecurityMiddleware)


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # FastAPI's default body includes submitted input, including invalid passwords.
    details = [
        {"type": item["type"], "loc": item["loc"][:-1] if item["type"] == "extra_forbidden" else item["loc"],
         "msg": item["msg"]}
        for item in exc.errors()
    ]
    return JSONResponse({"detail": details}, 422, headers={"Cache-Control": "no-store"})


@app.exception_handler(SQLAlchemyError)
async def database_unavailable(_request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.error("Database operation failed (%s)", type(exc).__name__)
    return JSONResponse({"detail": "Service temporarily unavailable."}, 503, headers={"Cache-Control": "no-store"})

# Static files
app.mount(
    "/static",
    StaticFiles(directory="app/static"),
    name="static",
)



@app.get("/health", tags=["System"])
async def health_check():
    return {"status": "healthy"}

app.include_router(auth.router, prefix="/auth", tags=["Authentication"])
app.include_router(impersonation.router)
app.include_router(dashboard.router)
app.include_router(academic_years.router)
app.include_router(academic_year_history.router)
app.include_router(closure_alerts.router)
app.include_router(clubs.router)
app.include_router(representative_applications.router)
app.include_router(activities.router)
app.include_router(publications.router)
