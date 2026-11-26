"""The ingestion API. Start it with:  uvicorn main:app --port 8100

The background worker is a separate process (python worker.py).
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

import auth
import limits
import models  # noqa: F401  (registers the tables)
from database import Base, engine, migrate_schema
from routes import router
from common.log import set_request_id, setup_logging
import time

logger = logging.getLogger(__name__)

setup_logging("ingestion")


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.init_from_env()  # refuses to start without a token (see common/service_auth.py)

    limits.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    migrate_schema()

    yield


# No /docs or /openapi.json: this is an internal service, not a public API
app = FastAPI(
    title="Info Mate Ingestion",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# [trace-middleware] one id per request, one result line per request
@app.middleware("http")
async def trace_requests(request: Request, call_next):
    rid = set_request_id(request.headers.get("x-request-id"))
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("%s %s crashed", request.method, request.url.path)
        raise
    ms = int((time.monotonic() - started) * 1000)
    level = logging.WARNING if response.status_code >= 400 else logging.INFO
    logger.log(level, "%s %s -> %d (%d ms)",
               request.method, request.url.path, response.status_code, ms)
    response.headers["X-Request-ID"] = rid
    return response


app.include_router(router)


@app.get("/health")
def health():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("INGEST_BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("INGEST_PORT", "8100")),
    )
