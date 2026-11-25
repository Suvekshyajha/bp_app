from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from history import router as history_router
import models  # noqa: F401  (registers the tables)
import chat_client
import documents
import ingest_client
import routes
import websocket
from auth import ALLOWED_ORIGINS
from database import Base, engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    # create_all skips tables that already exist, so it would never add a new
    # index to an old database. Create any missing ones here.
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            index.create(bind=engine, checkfirst=True)

    yield
    # Close the gRPC channel to the chat service
    await chat_client.close()
    # ...and the HTTP client to the ingestion service
    await ingest_client.close()


app = FastAPI(title="Chat orchestrator", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,  # the session cookie
    allow_methods=["*"],
    allow_headers=["*"],
)

# The frontend calls /api/health, /api/me, /api/login ...
app.include_router(routes.router, prefix="/api")

# Chat list and message history: /api/conversations ...
app.include_router(history_router, prefix="/api")

# Documents: /api/documents ...
app.include_router(documents.router, prefix="/api")

# WebSocket: /ws/chat
app.include_router(websocket.router)