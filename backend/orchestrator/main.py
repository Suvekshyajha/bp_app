from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from routes import router as rest_router
from websocket import router as websocket_router

from database import engine, Base
import models
from auth import ALLOWED_ORIGINS

load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Day2 BP Chat API",
    description="Chat API with hybrid retrieval",
    version="1.0.0",
    lifespan=lifespan,
)

# allow_credentials lets the browser send the session cookie.
# The origins must be listed explicitly (no "*").
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def block_foreign_origins(request: Request, call_next):
    """Cookies are sent automatically, so refuse changes from other websites."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")

        if origin and origin.rstrip("/") not in ALLOWED_ORIGINS:
            return JSONResponse(
                {"detail": "Origin not allowed."},
                status_code=403,
            )

    return await call_next(request)

app.include_router(rest_router, prefix="/api")
app.include_router(websocket_router)


@app.get("/")
def root():
    return {
        "message": "Day2 BP Chat API is running"
    }