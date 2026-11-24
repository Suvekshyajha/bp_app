"""Test setup for the ingestion service.

The environment is prepared BEFORE any service module is imported: a
throw-away encryption key and throw-away files, so tests never touch the
real ingest.db or uploads folder.
"""

import os
import tempfile

from cryptography.fernet import Fernet

_TEMP_DIR = tempfile.mkdtemp()

os.environ["CHAT_ENCRYPTION_KEYS"] = Fernet.generate_key().decode()
os.environ["INGEST_DB_PATH"] = os.path.join(_TEMP_DIR, "test_ingest.db")
os.environ["INGEST_UPLOAD_DIR"] = os.path.join(_TEMP_DIR, "uploads")
os.environ["INGEST_SERVICE_TOKEN"] = "test-token"
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("GROQ_API_KEY", "test-key")

import pytest


@pytest.fixture(autouse=True)
def whitespace_token_counter():
    """Count tokens as words, so no test ever loads the real tokenizer."""

    from chunking import tokens

    tokens.set_counter(lambda text: len(text.split()))
    yield
    tokens.set_counter(None)


@pytest.fixture
def session_factory():
    """An empty ingest.db for this test. Yields SessionLocal."""

    from database import Base, SessionLocal, engine, migrate_schema

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    migrate_schema()

    yield SessionLocal
