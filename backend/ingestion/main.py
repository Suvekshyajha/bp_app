"""The ingestion API. Start it with:  uvicorn main:app --port 8100

The background worker is a separate process (python worker.py).
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

import auth
import limits
import models  # noqa: F401  (registers the tables)
from database import Base, engine, migrate_schema
from routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


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
