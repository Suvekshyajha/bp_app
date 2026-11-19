from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import models  # noqa: F401  (registers the tables)
import retriever_client
import routes
import websocket
from auth import ALLOWED_ORIGINS
from database import Base, engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield
    # Close the long-lived gRPC streams to the retriever
    await retriever_client.close()


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

# WebSocket: /ws/chat
app.include_router(websocket.router)