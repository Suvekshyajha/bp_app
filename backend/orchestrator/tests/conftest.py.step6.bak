"""Shared test setup.

The tests never talk to the real chat service over the network, so the
`chat_client` module is replaced by a tiny fake *before* anything imports
it. Each test gets its own throw-away SQLite file for users and sessions,
so tests cannot affect each other or chat.db.
"""

import sys
import types
import uuid

import grpc
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

# ---- Fake chat service client (installed before any import) ----

fake_chat_client = types.ModuleType("chat_client")


class ChatServiceError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message)
        self.code = code
        self.message = message


class FakeChatService:
    """Scripted behaviour for the fake. Tests change these fields."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.tokens = ["Hel", "lo"]
        self.results = []
        self.error = None            # a ChatServiceError to raise first
        self.calls = []              # (user_id, conversation_id, query)
        self.conversations = {"items": [], "next_cursor": None}
        self.messages = {"items": [], "next_cursor": None}
        self.history_error = None


service = FakeChatService()


async def fake_chat(user_id, conversation_id, query):
    service.calls.append((user_id, conversation_id, query))

    if service.error:
        raise service.error

    yield "context", service.results

    for token in service.tokens:
        yield "token", token

    yield "done", {
        "answer": "".join(service.tokens),
        "user_message_id": "u1",
        "assistant_message_id": "a1",
    }


async def fake_list_conversations(user_id, limit, cursor):
    service.calls.append(("list_conversations", user_id, limit, cursor))

    if service.history_error:
        raise service.history_error

    return service.conversations


async def fake_list_messages(user_id, conversation_id, limit, cursor):
    service.calls.append(("list_messages", user_id, conversation_id, limit, cursor))

    if service.history_error:
        raise service.history_error

    return service.messages


async def fake_delete_conversation(user_id, conversation_id):
    service.calls.append(("delete_conversation", user_id, conversation_id))

    if service.history_error:
        raise service.history_error


async def fake_close():
    return None


fake_chat_client.ChatServiceError = ChatServiceError
fake_chat_client.chat = fake_chat
fake_chat_client.list_conversations = fake_list_conversations
fake_chat_client.list_messages = fake_list_messages
fake_chat_client.delete_conversation = fake_delete_conversation
fake_chat_client.close = fake_close
sys.modules["chat_client"] = fake_chat_client

# ---- Real app modules (safe to import now) ----

import auth  # noqa: E402
import history  # noqa: E402
import models  # noqa: E402
import websocket  # noqa: E402
from database import Base, get_db  # noqa: E402


@pytest.fixture(autouse=True)
def fake_service():
    """A clean scripted chat service for every test."""
    service.reset()
    return service


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,  # same as production
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture
def app(session_factory, engine, monkeypatch):
    application = FastAPI()
    application.include_router(history.router, prefix="/api")
    application.include_router(websocket.router)

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_db] = override_get_db

    # The websocket module opens its own sessions; point them at the test DB
    monkeypatch.setattr(websocket, "SessionLocal", session_factory)
    monkeypatch.setattr(websocket, "engine", engine)

    return application


@pytest.fixture
def client(app):
    return TestClient(app)


def make_user(db, name="alice"):
    user = models.User(
        id=str(uuid.uuid4()),
        username=name,
        email=f"{name}@example.com",
        password_hash="not-a-real-hash",
    )
    db.add(user)
    db.commit()
    return user


def login(client, db, user):
    """Create a real session and put its cookie on the test client."""
    token = auth.create_session(db, user.id)
    client.cookies.set(auth.SESSION_COOKIE_NAME, token)
    return token


@pytest.fixture
def user(db):
    return make_user(db)


@pytest.fixture
def logged_in(client, db, user):
    """A client that is logged in as `user`."""
    login(client, db, user)
    return client