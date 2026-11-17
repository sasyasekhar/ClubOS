from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.routes import auth
@asynccontextmanager
async def lifespan(app: FastAPI):

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