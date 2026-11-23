"""Shared test setup for the chat service.

No real Qdrant, embedding model or Groq is used: those parts are replaced by
small fakes inside each test. Each test gets its own throw-away SQLite file.
"""

import asyncio
import os
import sys
import types

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from cryptography.fernet import Fernet

# The real llm.py needs this at import time
os.environ.setdefault("GROQ_API_KEY", "test-key")

os.environ.setdefault("CHAT_ENCRYPTION_KEYS", Fernet.generate_key().decode())

# Replace the Qdrant and search modules before anything imports them, so
# these tests need no embedding model and no running Qdrant. (Run
# test_retrieval.py on its own: it tests the real retrieval.py.)
fake_qdrant_store = types.ModuleType("qdrant_store")
fake_qdrant_store.store_message = lambda **kwargs: "fake-id"
fake_qdrant_store.create_collection = lambda: None
sys.modules["qdrant_store"] = fake_qdrant_store

fake_retrieval = types.ModuleType("retrieval")
fake_retrieval.retrieve_messages = lambda **kwargs: []
sys.modules["retrieval"] = fake_retrieval

import repository  # noqa: E402
import turn  # noqa: E402
from database import Base  # noqa: E402


@pytest.fixture
def session_factory(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,  # same as production
    )
    Base.metadata.create_all(engine)

    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(repository, "SessionLocal", factory)

    yield factory

    engine.dispose()


@pytest.fixture
def db(session_factory):
    with session_factory() as session:
        yield session


class FakeModel:
    """A scripted stand-in for the language model."""

    def __init__(self):
        self.tokens = ["Hel", "lo"]
        self.fail_after: int | None = None   # raise after this many tokens
        self.block_after: int | None = None  # wait forever after this many tokens
        self.closed = False                  # was the stream closed properly?
        self.calls = []

    async def stream(self, query, context, history=None):
        self.calls.append({"query": query, "context": context, "history": history})

        try:
            for index, token in enumerate(self.tokens):
                if self.fail_after is not None and index == self.fail_after:
                    raise RuntimeError("model failed")

                if self.block_after is not None and index == self.block_after:
                    await asyncio.Event().wait()  # until cancelled

                yield token
        finally:
            self.closed = True


@pytest.fixture
def model(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(turn, "stream_response", fake.stream)
    return fake


@pytest.fixture
def indexed(monkeypatch):
    """Messages that were added to Qdrant."""
    stored = []

    def fake_store(user_id, conversation_id, role, content):
        stored.append((user_id, conversation_id, role, content))
        return "point-id"

    monkeypatch.setattr(turn, "store_message", fake_store)
    return stored


@pytest.fixture
def retrieved(monkeypatch):
    """Set `retrieved.results` to what the search should return."""

    class Search:
        results: list[dict] = []
        error: Exception | None = None
        calls: list[dict] = []

    search = Search()
    search.calls = []

    def fake_retrieve(**kwargs):
        search.calls.append(kwargs)

        if search.error:
            raise search.error

        return search.results

    monkeypatch.setattr(turn, "retrieve_messages", fake_retrieve)
    return search


async def drain_background():
    """Wait for background indexing started by a turn."""
    while turn._background:
        await asyncio.gather(*list(turn._background), return_exceptions=True)
