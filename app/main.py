from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.routes import (
    academic_year_history, academic_years, activities, auth, closure_alerts, clubs, dashboard, impersonation, publications,
    representative_applications,
)
from app.services.storage import get_attachment_storage


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_attachment_storage()
    yield

app = FastAPI(
    title="ClubOS",
    description="A multi-school club management application",
    version="0.1.0",
    lifespan=lifespan,
)

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
