"""Limits, status names and error codes for the ingestion service.

One place for every cap and name, so the API, the worker and the tests agree.
The numbers come from the migration plan (sections 6, 15 and 16).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default

    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a whole number.") from None

    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero.")

    return value


# ---------- Upload ----------

MAX_UPLOAD_MB = _int_env("MAX_UPLOAD_MB", 10)
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_DOCS_PER_USER = _int_env("MAX_DOCS_PER_USER", 20)

ALLOWED_EXTENSIONS = (".pdf", ".docx", ".txt", ".md")
SOURCE_TYPES = ("pdf", "docx", "txt", "md")

# ---------- Parsing ----------

MAX_PDF_PAGES = 300
MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024  # zip-bomb guard
MAX_TEXT_CHARS = 2_000_000
PARSE_TIMEOUT_SECONDS = 60

# ---------- Chunking ----------

# bge-base reads at most 512 tokens, so no chunk may pass this (decision D8).
HARD_MAX_CHUNK_TOKENS = 480

# My own choice (the plan names the error code but no number): stops one
# huge file from filling Qdrant. Change it here if it is too tight.
MAX_CHUNKS_PER_DOCUMENT = 2000

# ---------- Jobs ----------

JOB_TIMEOUT_SECONDS = _int_env("INGEST_JOB_TIMEOUT_SECONDS", 300)

# ---------- Statuses and stages ----------

PENDING = "pending"
PROCESSING = "processing"
READY = "ready"
FAILED = "failed"
DELETING = "deleting"

STATUSES = (PENDING, PROCESSING, READY, FAILED, DELETING)

STAGES = ("parsing", "analyzing", "chunking", "embedding", "storing")

# ---------- Safe error codes (never a stack trace) ----------

ERR_UNSUPPORTED = "unsupported_or_corrupt"
ERR_TOO_LARGE = "too_large"
ERR_NO_TEXT = "no_text"
ERR_TIMEOUT = "timeout"
ERR_TOO_MANY_CHUNKS = "too_many_chunks"

# ---------- Folders ----------

# Uploaded files wait here for the seconds a job runs, then are deleted.
UPLOAD_DIR = Path(os.getenv("INGEST_UPLOAD_DIR", "").strip() or BASE_DIR / "data" / "uploads")

# ---------- Stage 1: analysis ----------

ANALYSIS_ENABLED = os.getenv("INGEST_ANALYSIS_ENABLED", "true").strip().lower() != "false"
ANALYSIS_MODEL = os.getenv("INGEST_ANALYSIS_MODEL", "").strip() or "openai/gpt-oss-120b"
ANALYSIS_TIMEOUT_SECONDS = 30
ANALYSIS_MAX_TOKENS = 900

# At most this many tokens: skip the LLM and keep the whole text as one chunk (D7)
SINGLE_CHUNK_SKIP_TOKENS = 300
# A single_chunk recommendation above this is refused (guard rail)
SINGLE_CHUNK_MAX_TOKENS = 450

# ---------- Semantic chunking ----------

SEMANTIC_MAX_CHARS = _int_env("INGEST_SEMANTIC_MAX_CHARS", 80000)
SEMANTIC_MAX_SENTENCES = 2000
SEMANTIC_PERCENTILE = min(97, max(80, _int_env("INGEST_SEMANTIC_PERCENTILE", 90)))

# ---------- Embedding ----------

EMBED_BATCH_SIZE = 32
