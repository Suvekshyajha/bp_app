"""Shared test setup.

The tests never talk to the gRPC retriever or the LLM, so those two modules
are replaced by tiny fakes *before* anything imports them. Each test gets its
own throw-away SQLite file, so tests cannot affect each other or chat.db.
"""

import sys
import types
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

# ---- Fakes for the slow/external modules (installed before any import) ----

fake_retriever = types.ModuleType("retriever_client")


class RetrieverError(Exception):
    pass


async def fake_retrieve(query, user_id, conversation_id, limit):
    return []


async def fake_store(user_id, conversation_id, role, content):
    return None


async def fake_close():
    return None


fake_retriever.RetrieverError = RetrieverError
fake_retriever.retrieve = fake_retrieve
fake_retriever.store = fake_store
fake_retriever.close = fake_close
sys.modules["retriever_client"] = fake_retriever

fake_llm = types.ModuleType("llm")


async def fake_stream_response(query, results, history=None):
    for token in ("Hel", "lo"):
        yield token


fake_llm.stream_response = fake_stream_response
sys.modules["llm"] = fake_llm

# ---- Real app modules (safe to import now) ----

import auth  # noqa: E402
import history  # noqa: E402
import models  # noqa: E402
import websocket  # noqa: E402
from database import Base, get_db  # noqa: E402


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
    return clientcp