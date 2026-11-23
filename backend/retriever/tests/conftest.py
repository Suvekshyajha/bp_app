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
