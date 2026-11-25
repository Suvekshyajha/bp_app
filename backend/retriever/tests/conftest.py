"""Test setup for the chat service.

The environment is prepared BEFORE any service module is imported: a
throw-away encryption key and a throw-away SQLite file, so tests never touch
the real chat.db.
"""

import os
import tempfile

from cryptography.fernet import Fernet

os.environ["CHAT_ENCRYPTION_KEYS"] = Fernet.generate_key().decode()
os.environ["CHAT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test_chat.db")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("GROQ_API_KEY", "test-key")


# ---------------------------------------------------------------------------
# Shared fixtures for test_turn.py and test_grpc.py.
#
# They replace the model, the search and Qdrant with fakes by patching the
# names inside `turn` (the module that uses them). `turn` is imported inside
# the fixtures, not here, so test_chat_service.py can still install its own
# fakes before the service modules are first imported.
# ---------------------------------------------------------------------------

import asyncio

import pytest


async def drain_background():
    """Wait until the background indexing tasks started by a turn are done."""

    import turn

    while turn._background:
        await asyncio.gather(*list(turn._background), return_exceptions=True)


@pytest.fixture
def session_factory():
    """An empty database for this test. Yields SessionLocal."""

    from database import Base, SessionLocal, engine, migrate_schema

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    migrate_schema()

    yield SessionLocal


@pytest.fixture
def db(session_factory):
    """A session for looking at what was saved."""

    with session_factory() as session:
        yield session


class FakeModel:
    """Stands in for llm.stream_response.

    tokens       what it writes, one piece at a time
    fail_after   raise RuntimeError before writing token number N
    block_after  wait forever before writing token number N (until cancelled)
    calls        one dict per call: query, context, history
    closed       True once the stream has been closed or has finished
    """

    def __init__(self):
        self.tokens = ["Hel", "lo"]
        self.fail_after = None
        self.block_after = None
        self.calls = []
        self.closed = False

    async def stream(self, query, context, history=None, documents=None):
        self.calls.append({
            "query": query,
            "context": context,
            "history": history,
            "documents": documents,
        })
        self.closed = False

        try:
            for number, token in enumerate(self.tokens):
                if self.block_after == number:
                    await asyncio.Event().wait()

                if self.fail_after == number:
                    raise RuntimeError("model failed")

                yield token
        finally:
            self.closed = True


@pytest.fixture
def model(monkeypatch):
    import turn

    fake = FakeModel()
    monkeypatch.setattr(turn, "stream_response", fake.stream)
    return fake


@pytest.fixture
def indexed(monkeypatch):
    """Everything sent to Qdrant: (user_id, conversation_id, role, content)."""

    import turn

    stored = []

    def store_message(user_id, conversation_id, role, content, message_id=None):
        stored.append((user_id, conversation_id, role, content))
        return message_id

    monkeypatch.setattr(turn, "store_message", store_message)
    return stored


class FakeSearch:
    """Stands in for retrieval.retrieve_messages.

    results  what the search returns
    error    if set, the search raises it
    calls    the keyword arguments of every search
    """

    def __init__(self):
        self.results = []
        self.error = None
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)

        if self.error:
            raise self.error

        return list(self.results)


@pytest.fixture
def retrieved(monkeypatch):
    import turn

    fake = FakeSearch()
    monkeypatch.setattr(turn, "retrieve_messages", fake)
    return fake

@pytest.fixture(autouse=True)
def no_real_models(monkeypatch):
    """No test may load a real embedding model (slow, may use the network)."""
    import common.embeddings as emb

    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(1)
        raise RuntimeError("tried to load a real embedding model")

    monkeypatch.setattr(emb, "get_dense", refuse)
    monkeypatch.setattr(emb, "get_sparse", refuse)
    yield
    assert not attempts, "this test (or a background task it started) tried to load a real embedding model"


# ---------------------------------------------------------------------------
# Document search fakes (step 5). `turn` talks to doc_retrieval and to
# embed_query; both are replaced here, so no test needs Qdrant or a model.
# ---------------------------------------------------------------------------

DOC_VECTORS = {"dense": [1.0, 0.0], "sparse": {"indices": [1], "values": [1.0]}}


class FakeDocSearch:
    """Stands in for doc_retrieval and embed_query.

    states        document id -> "ready" / "processing" / "missing"
                  (an id that is not listed is "ready")
    results       the excerpts the search returns
    error         if set, the excerpt search raises it
    calls         the keyword arguments of every excerpt search
    state_calls   (user_id, document_ids) of every status check
    embeds        the text of every question that was embedded
    """

    def __init__(self):
        self.states = {}
        self.results = []
        self.error = None
        self.calls = []
        self.state_calls = []
        self.embeds = []

    def embed_query(self, text):
        self.embeds.append(text)
        return DOC_VECTORS

    def document_states(self, user_id, document_ids):
        self.state_calls.append((user_id, list(document_ids)))
        return {d: self.states.get(d, "ready") for d in document_ids}

    def retrieve_document_chunks(self, **kwargs):
        self.calls.append(kwargs)

        if self.error:
            raise self.error

        return list(self.results)


@pytest.fixture
def doc_search(monkeypatch):
    import turn

    fake = FakeDocSearch()
    monkeypatch.setattr(turn, "embed_query", fake.embed_query)
    monkeypatch.setattr(turn.doc_retrieval, "document_states", fake.document_states)
    monkeypatch.setattr(turn.doc_retrieval, "retrieve_document_chunks", fake.retrieve_document_chunks)
    return fake
