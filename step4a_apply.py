"""step4a_apply.py: first half of Step 4 (ingestion service).

Run from the PROJECT ROOT (the folder that holds backend\\ and frontend\\):

    python step4a_apply.py

It writes these files into backend\\ingestion\\:
  limits, doc_model, chunking\\tokens, database, models, repository,
  parsers, stats, pytest.ini, requirements*.txt and their tests.

A file that is missing or EMPTY is written. A file that already has content is
skipped (so nothing of yours is overwritten). Use --force to overwrite anyway.
"""

import sys
from pathlib import Path

FORCE = "--force" in sys.argv

FILES = [
    ("backend/ingestion/pytest.ini", r'''[pytest]
pythonpath = .
testpaths = tests
asyncio_mode = strict
asyncio_default_fixture_loop_scope = function
'''),
    ("backend/ingestion/requirements.txt", r'''# Same pins as the retriever (plan section 4).
qdrant-client==1.20.0
fastembed==0.9.1
sentence-transformers==6.3.0
torch==2.15.0
cryptography==50.0.2
SQLAlchemy==2.1.9
fastapi==0.163.2
uvicorn==0.54.0
httpx==0.28.1

# Pin these three with == after you install them:
#   pip freeze | findstr /i "pypdf python-docx python-multipart groq"
pypdf
python-docx
python-multipart
groq
'''),
    ("backend/ingestion/requirements-dev.txt", r'''-r requirements.txt
pytest
pytest-asyncio
'''),
    ("backend/ingestion/limits.py", r'''"""Limits, status names and error codes for the ingestion service.

One place for every cap and name, so the API, the worker and the tests agree.
The numbers come from the migration plan (sections 6, 15 and 16).
"""

import os


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
'''),
    ("backend/ingestion/doc_model.py", r'''"""Plain data classes shared by the parser, the stats and the chunkers.

They are plain dataclasses on purpose: the parser runs in a child process and
sends its result back, so everything here must be picklable.
"""

from dataclasses import dataclass, field

# Longest heading kept in a section path (one item).
MAX_PATH_ITEM_CHARS = 120


@dataclass
class Block:
    type: str  # heading | paragraph | list | table | code
    text: str  # table = rows joined by newline, cells by " | ", header first
    page: int | None = None  # 1-based, PDF only
    level: int | None = None  # heading level 1..6
    section_path: list[str] = field(default_factory=list)  # filled after parsing


@dataclass
class ParsedDocument:
    blocks: list[Block]
    source_type: str  # pdf | docx | txt | md
    page_count: int | None


@dataclass
class Chunk:
    index: int
    text: str
    tokens: int
    section_title: str | None  # " > ".join(last two path items), at most 120 chars
    page: int | None  # first page the chunk touches
    spans: list[tuple[int, int, int]]  # (block_index, start_char, end_char)


def assign_section_paths(blocks: list[Block]) -> None:
    """Fill `section_path` on every block from a stack of headings.

    A heading at level N removes every heading of level N or deeper from the
    stack, then adds itself. A heading's own path includes itself. Blocks
    before the first heading get an empty path.
    """

    stack: list[tuple[int, str]] = []

    for block in blocks:
        if block.type == "heading":
            level = block.level or 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, block.text.strip()[:MAX_PATH_ITEM_CHARS]))

        block.section_path = [title for _, title in stack]
'''),
    ("backend/ingestion/chunking/__init__.py", r'''"""Stage 2: deterministic chunkers. `run_chunking` is added in a later step."""
'''),
    ("backend/ingestion/chunking/tokens.py", r'''"""One token counter for all of ingestion.

Production counts with the bge tokenizer (common.embeddings.count_tokens).
That import loads a model, so it happens on first use and never at import,
and tests replace the counter with a cheap one through `set_counter()`.
"""

from typing import Callable

_counter: Callable[[str], int] | None = None


def set_counter(counter: Callable[[str], int] | None) -> None:
    """Use `counter` instead of the real tokenizer. None restores the real one."""

    global _counter
    _counter = counter


def count_tokens(text: str) -> int:
    if _counter is not None:
        return _counter(text)

    from common.embeddings import count_tokens as real_count_tokens

    return real_count_tokens(text)
'''),
    ("backend/ingestion/database.py", r'''import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool


BASE_DIR = Path(__file__).resolve().parent

# The ingestion service owns this file (documents and their job state).
# It never shares chat.db. Set INGEST_DB_PATH to keep it somewhere else.
DATABASE_PATH = Path(os.getenv("INGEST_DB_PATH", BASE_DIR / "ingest.db"))
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=NullPool,  # no pool: every connection is real, closed for real
)


# Runs on every new connection. The API and the worker are two processes that
# open this one file, so WAL (readers and a writer together) and a busy
# timeout (wait a moment instead of failing) are both needed.
@event.listens_for(engine, "connect")
def set_sqlite_pragmas(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


class Base(DeclarativeBase):
    pass


# create_all() makes missing TABLES but never adds a column to a table that
# already exists. Columns added after the first release are listed here and
# added to old databases when the service starts.
NEW_COLUMNS: list[tuple[str, str, str]] = [
    # (table, column, SQL type)
]


def migrate_schema() -> None:
    with engine.begin() as connection:
        for table, column, sql_type in NEW_COLUMNS:
            existing = {
                row[1]
                for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
            }

            if existing and column not in existing:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
                )
'''),
    ("backend/ingestion/models.py", r'''from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, String, UniqueConstraint

from common.crypto import EncryptedText
from database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    """One uploaded file. The job state lives in `status` and `stage`."""

    __tablename__ = "documents"

    # uuid5 of "user_id:file_hash" (see repository.make_document_id)
    id = Column(String, primary_key=True)

    # No ForeignKey: users live in the orchestrator's database.
    user_id = Column(String, nullable=False)
    file_hash = Column(String(64), nullable=False)  # sha256 hex

    # Encrypted at rest (see common/crypto.py)
    filename = Column(EncryptedText, nullable=False)

    source_type = Column(String, nullable=False)  # pdf, docx, txt, md

    # pending, processing, ready, failed, deleting
    status = Column(String, nullable=False, default="pending")

    # parsing, analyzing, chunking, embedding, storing (progress for the UI)
    stage = Column(String, nullable=True)

    # A short safe code only, never a stack trace
    error = Column(String, nullable=True)

    chunk_count = Column(Integer, nullable=False, default=0)

    # Both describe the document, so both are encrypted at rest.
    # analysis: the validated analysis JSON (the cache, decision D9)
    # report: the chunking report JSON
    analysis = Column(EncryptedText, nullable=True)
    report = Column(EncryptedText, nullable=True)

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow)
    started_at = Column(DateTime, nullable=True)

    __table_args__ = (
        # The same user uploading the same file again finds this row
        UniqueConstraint("user_id", "file_hash", name="uq_documents_user_hash"),
        # The worker's "oldest pending job" query
        Index("ix_documents_status_created", "status", "created_at"),
        # Lists a user's documents
        Index("ix_documents_user", "user_id"),
    )
'''),
    ("backend/ingestion/repository.py", r'''"""Everything that reads or writes ingest.db.

These are plain synchronous functions, each with its own short-lived session.
Callers inside the event loop call them with `asyncio.to_thread(...)`.

Every function that takes a user_id filters by it, so one user can never read
or change another user's document. Updates made by the worker only apply while
the row is still in the expected status, so a delete that happens in the
middle of a job is never overwritten.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from common.settings import DOC_NAMESPACE
from database import SessionLocal
from limits import DELETING, FAILED, PENDING, PROCESSING, READY
from models import Document


def make_document_id(user_id: str, file_hash: str) -> str:
    """The same user and the same file always give the same id."""

    return str(uuid.uuid5(DOC_NAMESPACE, f"{user_id}:{file_hash}"))


def _now() -> datetime:
    # SQLite keeps times without a timezone label, so everything is UTC
    # and naive, written and compared the same way.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_dict(row: Document) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "file_hash": row.file_hash,
        "filename": row.filename,
        "source_type": row.source_type,
        "status": row.status,
        "stage": row.stage,
        "error": row.error,
        "chunk_count": row.chunk_count or 0,
        "analysis": row.analysis,
        "report": row.report,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "started_at": row.started_at,
    }


# ---------- API side ----------

def create_document(
    user_id: str, file_hash: str, filename: str, source_type: str
) -> tuple[dict, bool]:
    """Add a `pending` document. Returns (document, created).

    Same user and same file again: nothing new is added and `created` is
    False (the upload is idempotent). A `failed` one is put back to `pending`
    and `created` is True, so the caller knows work was queued again.
    """

    document_id = make_document_id(user_id, file_hash)
    now = _now()

    with SessionLocal() as db:
        db.add(
            Document(
                id=document_id,
                user_id=user_id,
                file_hash=file_hash,
                filename=filename,
                source_type=source_type,
                status=PENDING,
                chunk_count=0,
                created_at=now,
                updated_at=now,
            )
        )

        try:
            db.commit()
            return _to_dict(db.get(Document, document_id)), True

        except IntegrityError:
            db.rollback()

        row = db.get(Document, document_id)
        if row is None or row.user_id != user_id:
            raise RuntimeError("Could not add the document.")

        if row.status == FAILED:
            row.status = PENDING
            row.stage = None
            row.error = None
            row.started_at = None
            row.updated_at = now
            db.commit()
            return _to_dict(row), True

        return _to_dict(row), False


def count_user_documents(user_id: str) -> int:
    with SessionLocal() as db:
        return db.execute(
            select(func.count()).select_from(Document).where(Document.user_id == user_id)
        ).scalar_one()


def get_document(user_id: str, document_id: str) -> dict | None:
    """One document, or None when it is missing OR belongs to someone else."""

    with SessionLocal() as db:
        row = db.execute(
            select(Document).where(
                Document.id == document_id, Document.user_id == user_id
            )
        ).scalar_one_or_none()
        return _to_dict(row) if row else None


def list_documents(user_id: str) -> list[dict]:
    with SessionLocal() as db:
        rows = db.execute(
            select(Document)
            .where(Document.user_id == user_id)
            .order_by(Document.created_at.desc(), Document.id)
        ).scalars()
        return [_to_dict(row) for row in rows]


def mark_deleting(user_id: str, document_id: str) -> bool:
    """First step of a delete. False when it is missing or not the user's."""

    with SessionLocal() as db:
        result = db.execute(
            update(Document)
            .where(Document.id == document_id, Document.user_id == user_id)
            .values(status=DELETING, updated_at=_now())
        )
        db.commit()
        return result.rowcount > 0


def delete_row(document_id: str) -> None:
    with SessionLocal() as db:
        row = db.get(Document, document_id)
        if row is not None:
            db.delete(row)
            db.commit()


# ---------- Worker side ----------

def get_document_by_id(document_id: str) -> dict | None:
    """For the worker only. It has no user, it works from a claimed job."""

    with SessionLocal() as db:
        row = db.get(Document, document_id)
        return _to_dict(row) if row else None


def claim_next_job() -> dict | None:
    """Take the oldest `pending` job, atomically.

    One UPDATE picks the row and marks it `processing`, so two workers can
    never take the same job.
    """

    inner = aliased(Document)
    oldest_pending = (
        select(inner.id)
        .where(inner.status == PENDING)
        .order_by(inner.created_at, inner.id)
        .limit(1)
        .scalar_subquery()
    )
    now = _now()

    with SessionLocal() as db:
        claimed = db.execute(
            update(Document)
            .where(Document.id == oldest_pending, Document.status == PENDING)
            .values(status=PROCESSING, started_at=now, updated_at=now)
            .returning(Document.id)
        ).scalar_one_or_none()
        db.commit()

        if claimed is None:
            return None

        return _to_dict(db.get(Document, claimed))


def requeue_stale(max_age_seconds: int) -> int:
    """Put `processing` jobs that started too long ago back to `pending`.

    This recovers jobs from a worker that crashed. Returns how many.
    """

    cutoff = _now() - timedelta(seconds=max_age_seconds)

    with SessionLocal() as db:
        result = db.execute(
            update(Document)
            .where(Document.status == PROCESSING, Document.started_at < cutoff)
            .values(status=PENDING, stage=None, started_at=None, updated_at=_now())
        )
        db.commit()
        return result.rowcount


def set_stage(document_id: str, stage: str) -> bool:
    """Show progress. Only applies while the job is still `processing`."""

    with SessionLocal() as db:
        result = db.execute(
            update(Document)
            .where(Document.id == document_id, Document.status == PROCESSING)
            .values(stage=stage, updated_at=_now())
        )
        db.commit()
        return result.rowcount > 0


def save_analysis(document_id: str, analysis_json: str) -> None:
    with SessionLocal() as db:
        db.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(analysis=analysis_json, updated_at=_now())
        )
        db.commit()


def save_report(document_id: str, report_json: str) -> None:
    with SessionLocal() as db:
        db.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(report=report_json, updated_at=_now())
        )
        db.commit()


def set_ready(document_id: str, chunk_count: int) -> bool:
    """Last step of a job. False if the document was deleted meanwhile."""

    with SessionLocal() as db:
        result = db.execute(
            update(Document)
            .where(Document.id == document_id, Document.status == PROCESSING)
            .values(
                status=READY,
                stage=None,
                error=None,
                chunk_count=chunk_count,
                updated_at=_now(),
            )
        )
        db.commit()
        return result.rowcount > 0


def set_failed(document_id: str, error_code: str) -> bool:
    """Record a safe error code. A `deleting` document is left alone."""

    with SessionLocal() as db:
        result = db.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.status.in_([PENDING, PROCESSING]),
            )
            .values(status=FAILED, stage=None, error=error_code, updated_at=_now())
        )
        db.commit()
        return result.rowcount > 0
'''),
    ("backend/ingestion/parsers.py", r'''"""Turn an uploaded file into typed blocks (heading, paragraph, list, table, code).

Rules for each file type are in the migration plan, section 15.1. The parser
never claims a structure the text does not show: a PDF has no real headings,
so only a clear short title-like line on its own becomes one.

Two entry points:
- parse_file()      does the work. It must run in a child process.
- parse_in_child()  runs parse_file() in a child process with a timeout.

The libraries (pypdf, python-docx) are imported inside the functions that need
them, so the API process can use check_file_signature() without loading them.
Any library error is turned into a short safe code, never a stack trace.
"""

import multiprocessing
import os
import re
import zipfile

import limits
from doc_model import Block, ParsedDocument, assign_section_paths


class ParseError(Exception):
    """The file cannot be used. `code` is one of the safe codes in limits.py."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_EXTENSION_TO_TYPE = {".pdf": "pdf", ".docx": "docx", ".txt": "txt", ".md": "md"}


def source_type_from_filename(filename: str) -> str | None:
    extension = os.path.splitext(filename.lower())[1]
    return _EXTENSION_TO_TYPE.get(extension)


# ---------- Checks that do not need a parsing library ----------

def _read_text(path: str) -> str:
    with open(path, "rb") as file:
        raw = file.read()

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ParseError(limits.ERR_UNSUPPORTED) from None

    if len(text) > limits.MAX_TEXT_CHARS:
        raise ParseError(limits.ERR_TOO_LARGE)

    return text.replace("\r\n", "\n").replace("\r", "\n")


def check_file_signature(path: str, source_type: str) -> None:
    """Check the real content, not just the extension (a .exe renamed to .pdf).

    PDF starts with %PDF-. DOCX is a zip that holds word/document.xml. Text
    must decode as UTF-8 after the BOM is removed.
    """

    try:
        if source_type == "pdf":
            with open(path, "rb") as file:
                if file.read(5) != b"%PDF-":
                    raise ParseError(limits.ERR_UNSUPPORTED)

        elif source_type == "docx":
            if not zipfile.is_zipfile(path):
                raise ParseError(limits.ERR_UNSUPPORTED)
            with zipfile.ZipFile(path) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise ParseError(limits.ERR_UNSUPPORTED)

        elif source_type in ("txt", "md"):
            _read_text(path)

        else:
            raise ParseError(limits.ERR_UNSUPPORTED)

    except ParseError:
        raise
    except Exception:
        raise ParseError(limits.ERR_UNSUPPORTED) from None


# ---------- Shared helpers ----------

def _split_segments(lines: list[str]) -> list[list[str]]:
    """Group lines into runs separated by blank lines."""

    segments: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if line.strip():
            current.append(line.rstrip())
        elif current:
            segments.append(current)
            current = []

    if current:
        segments.append(current)

    return segments


_PLAIN_LIST_LINE = re.compile(r"^\s*[-*]\s+\S")


def _paragraphs_and_lists(lines: list[str], page: int | None = None) -> list[Block]:
    """Lines with no blank line inside: runs of list lines and runs of others."""

    blocks: list[Block] = []
    run: list[str] = []
    run_is_list = False

    def flush() -> None:
        if run:
            kind = "list" if run_is_list else "paragraph"
            blocks.append(Block(kind, "\n".join(s.strip() for s in run), page=page))
            run.clear()

    for line in lines:
        is_list = bool(_PLAIN_LIST_LINE.match(line))
        if run and is_list != run_is_list:
            flush()
        run_is_list = is_list
        run.append(line)

    flush()
    return blocks


# ---------- txt ----------

def _parse_txt(path: str) -> tuple[list[Block], int | None]:
    blocks: list[Block] = []

    for segment in _split_segments(_read_text(path).split("\n")):
        blocks.extend(_paragraphs_and_lists(segment))

    return blocks, None


# ---------- md ----------

_MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
_MD_FENCE = re.compile(r"^\s*(```|~~~)")
_MD_LIST = re.compile(r"^\s*(?:[-*]|\d+\.)\s+\S")
_MD_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def _md_row(line: str) -> str:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return " | ".join(cells)


def _parse_md(path: str) -> tuple[list[Block], int | None]:
    lines = _read_text(path).split("\n")
    blocks: list[Block] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(Block("paragraph", "\n".join(paragraph)))
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]

        if not line.strip():
            flush_paragraph()
            i += 1
            continue

        fence = _MD_FENCE.match(line)
        if fence:
            flush_paragraph()
            marker = fence.group(1)
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith(marker):
                code.append(lines[i])
                i += 1
            i += 1  # the closing fence (or the end of the file)
            text = "\n".join(code).strip("\n")
            if text.strip():
                blocks.append(Block("code", text))
            continue

        heading = _MD_HEADING.match(line)
        if heading:
            flush_paragraph()
            if heading.group(2).strip():
                blocks.append(
                    Block("heading", heading.group(2).strip(), level=len(heading.group(1)))
                )
            i += 1
            continue

        is_table = (
            "|" in line
            and i + 1 < len(lines)
            and "|" in lines[i + 1]
            and bool(_MD_TABLE_SEPARATOR.match(lines[i + 1]))
        )
        if is_table:
            flush_paragraph()
            rows = [_md_row(line)]
            i += 2  # the header and the ---|--- line
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                rows.append(_md_row(lines[i]))
                i += 1
            blocks.append(Block("table", "\n".join(rows)))
            continue

        if _MD_LIST.match(line):
            flush_paragraph()
            items: list[str] = []
            while i < len(lines) and lines[i].strip() and (
                _MD_LIST.match(lines[i]) or lines[i][:1].isspace()
            ):
                items.append(lines[i].strip())
                i += 1
            blocks.append(Block("list", "\n".join(items)))
            continue

        paragraph.append(line.strip())
        i += 1

    flush_paragraph()
    return blocks, None


# ---------- docx ----------

_HEADING_STYLE = re.compile(r"^heading\s+(\d)$", re.IGNORECASE)


def _docx_heading_level(style_name: str) -> int | None:
    if style_name.strip().lower() == "title":
        return 1

    match = _HEADING_STYLE.match(style_name.strip())
    if match:
        return min(max(int(match.group(1)), 1), 6)

    return None


def _parse_docx(path: str) -> tuple[list[Block], int | None]:
    # Zip-bomb guard: check the unpacked size BEFORE python-docx reads it
    with zipfile.ZipFile(path) as archive:
        unpacked = sum(info.file_size for info in archive.infolist())
    if unpacked > limits.MAX_DOCX_UNCOMPRESSED_BYTES:
        raise ParseError(limits.ERR_TOO_LARGE)

    from docx import Document as DocxDocument
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = DocxDocument(path)
    blocks: list[Block] = []
    list_items: list[str] = []

    def flush_list() -> None:
        if list_items:
            blocks.append(Block("list", "\n".join(list_items)))
            list_items.clear()

    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]

        if tag == "p":
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue

            try:
                style_name = paragraph.style.name or ""
            except Exception:
                style_name = ""

            level = _docx_heading_level(style_name)
            if level:
                flush_list()
                blocks.append(Block("heading", text, level=level))
            elif style_name.lower().startswith("list"):
                list_items.append(text)
            else:
                flush_list()
                blocks.append(Block("paragraph", text))

        elif tag == "tbl":
            flush_list()
            rows: list[str] = []
            for row in Table(child, document).rows:
                cells: list[str] = []
                previous = None
                for cell in row.cells:
                    if cell._tc is previous:  # a merged cell shows up twice
                        continue
                    previous = cell._tc
                    cells.append(cell.text.strip().replace("\n", " "))
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                blocks.append(Block("table", "\n".join(rows)))

    flush_list()
    return blocks, None


# ---------- pdf ----------

_SMALL_WORDS = {"a", "an", "the", "of", "in", "on", "and", "or", "for", "to", "at", "by", "with", "vs"}


def _looks_like_pdf_heading(line: str) -> bool:
    """A short line, ALL CAPS or Title Case, with no final period."""

    text = line.strip()
    if not text or len(text) > 80 or text.endswith("."):
        return False
    if not any(character.isalpha() for character in text):
        return False
    if text.isupper():
        return True

    words = text.split()
    if len(words) > 12 or not words[0][:1].isupper():
        return False

    for word in words:
        first = word[:1]
        if first.isalpha() and not first.isupper() and word.lower() not in _SMALL_WORDS:
            return False

    return True


_TABLE_GAP = re.compile(r"\t| {2,}")


def _is_table_line(line: str) -> bool:
    """Several runs of two or more spaces (or tabs) inside the line."""

    return len(_TABLE_GAP.findall(line.strip())) >= 2


def _pdf_segment_blocks(lines: list[str], page: int) -> list[Block]:
    if len(lines) == 1 and _looks_like_pdf_heading(lines[0]):
        return [Block("heading", lines[0].strip(), page=page, level=1)]

    blocks: list[Block] = []
    text_lines: list[str] = []
    table_lines: list[str] = []

    def flush_text() -> None:
        if text_lines:
            blocks.append(
                Block("paragraph", "\n".join(s.strip() for s in text_lines), page=page)
            )
            text_lines.clear()

    def flush_table() -> None:
        # A table needs 3 or more lines in a row, otherwise it is plain text
        if len(table_lines) >= 3:
            flush_text()
            rows = [" | ".join(_TABLE_GAP.split(row.strip())) for row in table_lines]
            blocks.append(Block("table", "\n".join(rows), page=page))
        else:
            text_lines.extend(table_lines)
        table_lines.clear()

    for line in lines:
        if _is_table_line(line):
            table_lines.append(line)
        else:
            flush_table()
            text_lines.append(line)

    flush_table()
    flush_text()
    return blocks


def _parse_pdf(path: str) -> tuple[list[Block], int | None]:
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ParseError(limits.ERR_UNSUPPORTED)

    page_count = len(reader.pages)
    if page_count > limits.MAX_PDF_PAGES:
        raise ParseError(limits.ERR_TOO_LARGE)

    blocks: list[Block] = []
    total_chars = 0

    for number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").replace("\r\n", "\n").replace("\r", "\n")

        total_chars += len(text)
        if total_chars > limits.MAX_TEXT_CHARS:
            raise ParseError(limits.ERR_TOO_LARGE)

        for segment in _split_segments(text.split("\n")):
            blocks.extend(_pdf_segment_blocks(segment, number))

    return blocks, page_count


# ---------- Entry points ----------

_PARSERS = {
    "txt": _parse_txt,
    "md": _parse_md,
    "docx": _parse_docx,
    "pdf": _parse_pdf,
}


def parse_file(path: str, source_type: str) -> ParsedDocument:
    """Parse one file. Raises ParseError with a safe code, nothing else."""

    if source_type not in _PARSERS:
        raise ParseError(limits.ERR_UNSUPPORTED)

    check_file_signature(path, source_type)

    try:
        blocks, page_count = _PARSERS[source_type](path)
    except ParseError:
        raise
    except Exception:
        # Library messages can hold file content, so none of it is passed on
        raise ParseError(limits.ERR_UNSUPPORTED) from None

    if sum(len(block.text) for block in blocks) > limits.MAX_TEXT_CHARS:
        raise ParseError(limits.ERR_TOO_LARGE)

    assign_section_paths(blocks)
    return ParsedDocument(blocks=blocks, source_type=source_type, page_count=page_count)


def _child_main(connection, path: str, source_type: str) -> None:
    try:
        connection.send(("ok", parse_file(path, source_type)))
    except ParseError as error:
        connection.send(("error", error.code))
    except BaseException:
        connection.send(("error", limits.ERR_UNSUPPORTED))
    finally:
        connection.close()


def parse_in_child(path: str, source_type: str, timeout: float | None = None, target=_child_main) -> ParsedDocument:
    """Run parse_file() in its own process and kill it if it takes too long.

    A broken or hostile file can hang or crash a parser. In a child process
    that costs one process, not the worker. `target` exists only so tests can
    swap in a slow function.
    """

    wait = limits.PARSE_TIMEOUT_SECONDS if timeout is None else timeout

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=target, args=(sender, path, source_type), daemon=True)
    process.start()
    sender.close()

    try:
        if not receiver.poll(wait):
            raise ParseError(limits.ERR_TIMEOUT)

        try:
            kind, value = receiver.recv()
        except (EOFError, OSError):
            # The child died without answering (a crash)
            raise ParseError(limits.ERR_UNSUPPORTED) from None

        if kind == "ok":
            return value

        raise ParseError(value)

    finally:
        receiver.close()
        if process.is_alive():
            process.kill()
        process.join(5)
'''),
    ("backend/ingestion/stats.py", r'''"""Deterministic statistics about a parsed document (plan section 15.2).

Pure Python, no model calls. The same input always gives the same numbers.
Stage 1 (the analyzer) sends these numbers to the LLM instead of the file.
"""

from chunking.tokens import count_tokens
from doc_model import ParsedDocument

BLOCK_TYPES = ("heading", "paragraph", "list", "table", "code")


def compute_stats(parsed: ParsedDocument) -> dict:
    counts = dict.fromkeys(BLOCK_TYPES, 0)
    chars = 0
    tokens = 0
    paragraph_tokens: list[int] = []
    heading_levels: list[int] = []

    for block in parsed.blocks:
        size = count_tokens(block.text)
        chars += len(block.text)
        tokens += size

        if block.type in counts:
            counts[block.type] += 1

        if block.type == "paragraph":
            paragraph_tokens.append(size)
        elif block.type == "heading":
            heading_levels.append(block.level or 1)

    total_blocks = sum(counts.values())

    # Depth = how many levels the headings span (H1 to H3 is 3, only H3s is 1)
    heading_depth = (
        max(heading_levels) - min(heading_levels) + 1 if heading_levels else 0
    )

    # At least 3 headings, and either 2 or more levels or 3 headings sharing one
    same_level_max = max(
        (heading_levels.count(level) for level in set(heading_levels)), default=0
    )
    has_hierarchy = len(heading_levels) >= 3 and (
        len(set(heading_levels)) >= 2 or same_level_max >= 3
    )

    return {
        "source_type": parsed.source_type,
        "chars": chars,
        "tokens": tokens,
        "pages": parsed.page_count,
        "blocks": counts,
        "heading_depth": heading_depth,
        "avg_paragraph_tokens": (
            round(sum(paragraph_tokens) / len(paragraph_tokens)) if paragraph_tokens else 0
        ),
        "max_paragraph_tokens": max(paragraph_tokens, default=0),
        "list_ratio": round(counts["list"] / total_blocks, 2) if total_blocks else 0.0,
        "has_hierarchy": has_hierarchy,
    }
'''),
    ("backend/ingestion/tests/conftest.py", r'''"""Test setup for the ingestion service.

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
'''),
    ("backend/ingestion/tests/test_stats.py", r'''from doc_model import Block, ParsedDocument
from stats import compute_stats


def _doc(blocks, source_type="md", pages=None):
    return ParsedDocument(blocks=blocks, source_type=source_type, page_count=pages)


def test_counts_blocks_by_type():
    parsed = _doc(
        [
            Block("heading", "Intro", level=1),
            Block("paragraph", "one two three"),
            Block("paragraph", "four five"),
            Block("list", "- a\n- b"),
            Block("table", "a | b\n1 | 2"),
            Block("code", "print(1)"),
        ]
    )

    stats = compute_stats(parsed)

    assert stats["blocks"] == {
        "heading": 1, "paragraph": 2, "list": 1, "table": 1, "code": 1,
    }


def test_paragraph_sizes_use_the_token_counter():
    parsed = _doc([Block("paragraph", "a b c d"), Block("paragraph", "a b")])

    stats = compute_stats(parsed)

    assert stats["avg_paragraph_tokens"] == 3
    assert stats["max_paragraph_tokens"] == 4
    assert stats["tokens"] == 6


def test_chars_pages_and_source_type():
    parsed = _doc([Block("paragraph", "hello")], source_type="pdf", pages=7)

    stats = compute_stats(parsed)

    assert stats["chars"] == 5
    assert stats["pages"] == 7
    assert stats["source_type"] == "pdf"


def test_list_ratio_is_lists_over_all_blocks():
    parsed = _doc(
        [Block("list", "- a"), Block("paragraph", "x"), Block("paragraph", "y"), Block("paragraph", "z")]
    )

    assert compute_stats(parsed)["list_ratio"] == 0.25


def test_heading_depth_and_hierarchy():
    parsed = _doc(
        [
            Block("heading", "A", level=1),
            Block("heading", "B", level=2),
            Block("heading", "C", level=2),
        ]
    )

    stats = compute_stats(parsed)

    assert stats["heading_depth"] == 2
    assert stats["has_hierarchy"] is True


def test_two_headings_are_not_a_hierarchy():
    parsed = _doc([Block("heading", "A", level=1), Block("heading", "B", level=2)])

    assert compute_stats(parsed)["has_hierarchy"] is False


def test_empty_document_gives_zeros():
    stats = compute_stats(_doc([]))

    assert stats["tokens"] == 0
    assert stats["list_ratio"] == 0.0
    assert stats["heading_depth"] == 0
    assert stats["avg_paragraph_tokens"] == 0
    assert stats["has_hierarchy"] is False


def test_same_input_gives_same_stats():
    blocks = [Block("heading", "A", level=1), Block("paragraph", "one two")]

    assert compute_stats(_doc(blocks)) == compute_stats(_doc(blocks))
'''),
    ("backend/ingestion/tests/test_parsers.py", r'''import io
import time
import zipfile

import pytest

import limits
import parsers
from parsers import ParseError


# ---------- helpers ----------

def write(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return str(path)


def make_pdf(pages):
    """A tiny valid PDF. `pages` is a list of pages, each a list of text lines."""

    count = len(pages)
    page_ids = [4 + 2 * i for i in range(count)]
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, lines in enumerate(pages):
        content_id = 5 + 2 * i
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_id} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
            ).encode()
        )
        stream = "BT /F1 12 Tf 72 720 Td 14 TL\n"
        stream += "".join(f"({line}) Tj T*\n" for line in lines) + "ET"
        data = stream.encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def make_docx(tmp_path, build):
    from docx import Document

    document = Document()
    build(document)
    path = tmp_path / "file.docx"
    document.save(str(path))
    return str(path)


# ---------- txt ----------

def test_txt_gives_paragraphs_and_lists(tmp_path):
    path = write(tmp_path, "a.txt", "First paragraph.\n\n- item one\n- item two\n\nLast one.")

    parsed = parsers.parse_file(path, "txt")

    assert [b.type for b in parsed.blocks] == ["paragraph", "list", "paragraph"]
    assert parsed.blocks[1].text == "- item one\n- item two"
    assert parsed.source_type == "txt"
    assert parsed.page_count is None


def test_txt_with_a_byte_order_mark_and_windows_line_endings(tmp_path):
    path = write(tmp_path, "a.txt", b"\xef\xbb\xbfHello\r\n\r\nWorld")

    parsed = parsers.parse_file(path, "txt")

    assert [b.text for b in parsed.blocks] == ["Hello", "World"]


def test_txt_that_is_not_utf8_is_rejected(tmp_path):
    path = write(tmp_path, "a.txt", b"\xff\xfe\x00bad")

    with pytest.raises(ParseError) as error:
        parsers.parse_file(path, "txt")

    assert error.value.code == limits.ERR_UNSUPPORTED


def test_too_much_text_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "MAX_TEXT_CHARS", 20)
    path = write(tmp_path, "a.txt", "x" * 21)

    with pytest.raises(ParseError) as error:
        parsers.parse_file(path, "txt")

    assert error.value.code == limits.ERR_TOO_LARGE


# ---------- md ----------

MARKDOWN = """# Title

Intro line one
intro line two

## Part A

- first
- second
1. numbered

```python
x = 1

y = 2
```

| Name | Qty |
| --- | --- |
| apple | 3 |
| pear | 5 |

### Detail

Closing words.
"""


def test_md_gives_typed_blocks(tmp_path):
    parsed = parsers.parse_file(write(tmp_path, "a.md", MARKDOWN), "md")

    kinds = [(b.type, b.level) for b in parsed.blocks]

    assert kinds == [
        ("heading", 1),
        ("paragraph", None),
        ("heading", 2),
        ("list", None),
        ("code", None),
        ("table", None),
        ("heading", 3),
        ("paragraph", None),
    ]


def test_md_details(tmp_path):
    parsed = parsers.parse_file(write(tmp_path, "a.md", MARKDOWN), "md")
    by_type = {}
    for block in parsed.blocks:
        by_type.setdefault(block.type, []).append(block)

    assert by_type["paragraph"][0].text == "Intro line one\nintro line two"
    assert by_type["code"][0].text == "x = 1\n\ny = 2"
    assert by_type["table"][0].text == "Name | Qty\napple | 3\npear | 5"
    assert by_type["list"][0].text.splitlines() == ["- first", "- second", "1. numbered"]


def test_md_section_paths_follow_the_heading_stack(tmp_path):
    parsed = parsers.parse_file(write(tmp_path, "a.md", MARKDOWN), "md")

    paths = [block.section_path for block in parsed.blocks]

    assert paths[0] == ["Title"]  # a heading's own path includes itself
    assert paths[3] == ["Title", "Part A"]
    assert paths[6] == ["Title", "Part A", "Detail"]
    assert paths[7] == ["Title", "Part A", "Detail"]


def test_a_new_heading_at_the_same_level_replaces_the_old_one(tmp_path):
    path = write(tmp_path, "a.md", "# One\n\n## A\n\ntext\n\n## B\n\nmore\n\n# Two\n\nend")

    parsed = parsers.parse_file(path, "md")

    paths = [b.section_path for b in parsed.blocks if b.type == "paragraph"]
    assert paths == [["One", "A"], ["One", "B"], ["Two"]]


def test_text_before_any_heading_has_an_empty_path(tmp_path):
    parsed = parsers.parse_file(write(tmp_path, "a.md", "Hello\n\n# H\n\nBye"), "md")

    assert parsed.blocks[0].section_path == []


# ---------- docx ----------

def test_docx_gives_headings_lists_tables_in_order(tmp_path):
    def build(document):
        document.add_heading("Main title", level=1)
        document.add_paragraph("A normal paragraph.")
        document.add_heading("Sub part", level=2)
        document.add_paragraph("one", style="List Bullet")
        document.add_paragraph("two", style="List Bullet")
        table = document.add_table(rows=2, cols=2)
        table.rows[0].cells[0].text = "Name"
        table.rows[0].cells[1].text = "Qty"
        table.rows[1].cells[0].text = "apple"
        table.rows[1].cells[1].text = "3"
        document.add_paragraph("")  # empty paragraphs are skipped
        document.add_paragraph("The end.")

    parsed = parsers.parse_file(make_docx(tmp_path, build), "docx")

    assert [(b.type, b.level) for b in parsed.blocks] == [
        ("heading", 1),
        ("paragraph", None),
        ("heading", 2),
        ("list", None),
        ("table", None),
        ("paragraph", None),
    ]
    assert parsed.blocks[3].text == "one\ntwo"
    assert parsed.blocks[4].text == "Name | Qty\napple | 3"
    assert parsed.blocks[5].section_path == ["Main title", "Sub part"]


def test_docx_zip_bomb_is_rejected(tmp_path, monkeypatch):
    path = make_docx(tmp_path, lambda d: d.add_paragraph("hello"))
    monkeypatch.setattr(limits, "MAX_DOCX_UNCOMPRESSED_BYTES", 100)

    with pytest.raises(ParseError) as error:
        parsers.parse_file(path, "docx")

    assert error.value.code == limits.ERR_TOO_LARGE


def test_a_zip_that_is_not_a_docx_is_rejected(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "hi")
    path = write(tmp_path, "a.docx", buffer.getvalue())

    with pytest.raises(ParseError) as error:
        parsers.parse_file(path, "docx")

    assert error.value.code == limits.ERR_UNSUPPORTED


# ---------- pdf ----------

def test_pdf_blocks_carry_page_numbers(tmp_path):
    path = write(tmp_path, "a.pdf", make_pdf([["First page text here"], ["Second page text"]]))

    parsed = parsers.parse_file(path, "pdf")

    assert parsed.page_count == 2
    assert [b.page for b in parsed.blocks] == [1, 2]
    assert "First page text" in parsed.blocks[0].text
    assert "Second page text" in parsed.blocks[1].text


def test_pdf_over_the_page_cap_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "MAX_PDF_PAGES", 1)
    path = write(tmp_path, "a.pdf", make_pdf([["one"], ["two"]]))

    with pytest.raises(ParseError) as error:
        parsers.parse_file(path, "pdf")

    assert error.value.code == limits.ERR_TOO_LARGE


def test_corrupt_pdf_fails_cleanly(tmp_path):
    path = write(tmp_path, "a.pdf", b"%PDF-1.4\nthis is not really a pdf at all")

    with pytest.raises(ParseError) as error:
        parsers.parse_file(path, "pdf")

    assert error.value.code == limits.ERR_UNSUPPORTED


def test_pdf_heading_heuristic():
    looks = parsers._looks_like_pdf_heading

    assert looks("INTRODUCTION")
    assert looks("Methods And Data")
    assert looks("Results of the Study")
    assert not looks("This ends with a period.")
    assert not looks("this starts lowercase")
    assert not looks("42")
    assert not looks("x" * 81)


def test_pdf_segment_makes_heading_only_when_alone():
    alone = parsers._pdf_segment_blocks(["OVERVIEW"], 3)
    with_text = parsers._pdf_segment_blocks(["OVERVIEW", "Some text follows here"], 3)

    assert [(b.type, b.level, b.page) for b in alone] == [("heading", 1, 3)]
    assert [b.type for b in with_text] == ["paragraph"]


def test_pdf_table_needs_three_aligned_lines_in_a_row():
    table_lines = ["Item    Qty    Price", "Apple   3      1.20", "Pear    5      0.80"]

    blocks = parsers._pdf_segment_blocks(table_lines, 1)
    short = parsers._pdf_segment_blocks(table_lines[:2], 1)

    assert [b.type for b in blocks] == ["table"]
    assert blocks[0].text.splitlines()[0] == "Item | Qty | Price"
    assert [b.type for b in short] == ["paragraph"]


# ---------- signatures (what the API checks before it accepts a file) ----------

def test_wrong_magic_bytes_are_rejected(tmp_path):
    fake_pdf = write(tmp_path, "evil.pdf", b"MZ\x90\x00 this is an exe")

    with pytest.raises(ParseError) as error:
        parsers.check_file_signature(fake_pdf, "pdf")

    assert error.value.code == limits.ERR_UNSUPPORTED


def test_empty_file_is_rejected_for_pdf_and_docx(tmp_path):
    for name, kind in (("e.pdf", "pdf"), ("e.docx", "docx")):
        with pytest.raises(ParseError):
            parsers.check_file_signature(write(tmp_path, name, b""), kind)


def test_source_type_comes_from_the_extension():
    assert parsers.source_type_from_filename("Report.PDF") == "pdf"
    assert parsers.source_type_from_filename("a.docx") == "docx"
    assert parsers.source_type_from_filename("notes.md") == "md"
    assert parsers.source_type_from_filename("a.txt") == "txt"
    assert parsers.source_type_from_filename("virus.exe") is None
    assert parsers.source_type_from_filename("noextension") is None


def test_unknown_source_type_is_rejected(tmp_path):
    with pytest.raises(ParseError):
        parsers.parse_file(write(tmp_path, "a.txt", "hi"), "exe")


# ---------- child process ----------

def _sleepy(connection, path, source_type):
    time.sleep(30)


def test_parse_in_child_returns_the_parsed_document(tmp_path):
    path = write(tmp_path, "a.txt", "Hello there.\n\nSecond.")

    parsed = parsers.parse_in_child(path, "txt", timeout=60)

    assert [b.text for b in parsed.blocks] == ["Hello there.", "Second."]


def test_parse_in_child_reports_a_safe_error_code(tmp_path):
    path = write(tmp_path, "a.pdf", b"not a pdf")

    with pytest.raises(ParseError) as error:
        parsers.parse_in_child(path, "pdf", timeout=60)

    assert error.value.code == limits.ERR_UNSUPPORTED


def test_a_slow_parser_is_killed_and_fails_with_timeout(tmp_path):
    path = write(tmp_path, "a.txt", "hi")
    started = time.monotonic()

    with pytest.raises(ParseError) as error:
        parsers.parse_in_child(path, "txt", timeout=1, target=_sleepy)

    assert error.value.code == limits.ERR_TIMEOUT
    assert time.monotonic() - started < 15
'''),
    ("backend/ingestion/tests/test_repository.py", r'''from datetime import timedelta

import pytest
from sqlalchemy import text

import repository
from database import engine
from limits import DELETING, FAILED, PENDING, PROCESSING, READY

HASH_A = "a" * 64
HASH_B = "b" * 64


def add(user="user-1", file_hash=HASH_A, name="report.pdf", kind="pdf"):
    return repository.create_document(user, file_hash, name, kind)


def test_a_new_document_is_pending(session_factory):
    document, created = add()

    assert created is True
    assert document["status"] == PENDING
    assert document["filename"] == "report.pdf"
    assert document["chunk_count"] == 0
    assert document["id"] == repository.make_document_id("user-1", HASH_A)


def test_the_same_user_and_file_again_is_one_document(session_factory):
    first, created_first = add()
    second, created_second = add()

    assert created_first is True
    assert created_second is False
    assert first["id"] == second["id"]
    assert repository.count_user_documents("user-1") == 1


def test_two_users_with_the_same_file_get_two_documents(session_factory):
    mine, _ = add(user="user-1")
    theirs, _ = add(user="user-2")

    assert mine["id"] != theirs["id"]
    assert repository.count_user_documents("user-1") == 1
    assert repository.count_user_documents("user-2") == 1


def test_a_failed_document_is_queued_again(session_factory):
    document, _ = add()
    repository.claim_next_job()
    repository.set_failed(document["id"], "timeout")

    again, created = add()

    assert created is True
    assert again["status"] == PENDING
    assert again["error"] is None


def test_get_and_list_only_show_the_owners_documents(session_factory):
    mine, _ = add(user="user-1")
    add(user="user-2", file_hash=HASH_B)

    assert repository.get_document("user-1", mine["id"])["id"] == mine["id"]
    assert repository.get_document("user-2", mine["id"]) is None  # someone else's
    assert repository.get_document("user-1", "missing") is None
    assert [d["id"] for d in repository.list_documents("user-1")] == [mine["id"]]


def test_filename_and_analysis_are_encrypted_in_the_file(session_factory):
    document, _ = add(name="secret-plan.pdf")
    repository.save_analysis(document["id"], '{"strategy": "recursive"}')
    repository.save_report(document["id"], '{"chunk_count": 3}')

    with engine.connect() as connection:
        raw = connection.execute(
            text("SELECT filename, analysis, report FROM documents")
        ).one()

    assert "secret-plan" not in raw[0]
    assert "recursive" not in raw[1]
    assert "chunk_count" not in raw[2]

    again = repository.get_document("user-1", document["id"])
    assert again["filename"] == "secret-plan.pdf"
    assert again["analysis"] == '{"strategy": "recursive"}'
    assert again["report"] == '{"chunk_count": 3}'


def test_claim_takes_the_oldest_pending_job_once(session_factory):
    first, _ = add(file_hash=HASH_A)
    second, _ = add(file_hash=HASH_B)

    claimed_one = repository.claim_next_job()
    claimed_two = repository.claim_next_job()
    claimed_three = repository.claim_next_job()

    assert claimed_one["id"] == first["id"]
    assert claimed_one["status"] == PROCESSING
    assert claimed_one["started_at"] is not None
    assert claimed_two["id"] == second["id"]
    assert claimed_three is None


def test_stale_processing_jobs_go_back_to_pending(session_factory):
    document, _ = add()
    repository.claim_next_job()

    assert repository.requeue_stale(max_age_seconds=3600) == 0  # still fresh

    # make it old by moving started_at into the past
    from database import SessionLocal
    from models import Document

    with SessionLocal() as db:
        row = db.get(Document, document["id"])
        row.started_at = row.started_at - timedelta(hours=2)
        db.commit()

    assert repository.requeue_stale(max_age_seconds=3600) == 1
    assert repository.get_document_by_id(document["id"])["status"] == PENDING


def test_set_stage_and_set_ready_follow_the_job(session_factory):
    document, _ = add()
    repository.claim_next_job()

    assert repository.set_stage(document["id"], "parsing") is True
    assert repository.get_document_by_id(document["id"])["stage"] == "parsing"

    assert repository.set_ready(document["id"], 7) is True
    done = repository.get_document_by_id(document["id"])
    assert done["status"] == READY
    assert done["chunk_count"] == 7
    assert done["stage"] is None


def test_a_delete_during_a_job_is_not_overwritten(session_factory):
    document, _ = add()
    repository.claim_next_job()
    repository.mark_deleting("user-1", document["id"])

    assert repository.set_stage(document["id"], "embedding") is False
    assert repository.set_ready(document["id"], 5) is False
    assert repository.set_failed(document["id"], "timeout") is False
    assert repository.get_document_by_id(document["id"])["status"] == DELETING


def test_set_failed_records_a_safe_code(session_factory):
    document, _ = add()
    repository.claim_next_job()

    assert repository.set_failed(document["id"], "no_text") is True

    failed = repository.get_document_by_id(document["id"])
    assert failed["status"] == FAILED
    assert failed["error"] == "no_text"


def test_mark_deleting_refuses_someone_elses_document(session_factory):
    document, _ = add(user="user-1")

    assert repository.mark_deleting("user-2", document["id"]) is False
    assert repository.mark_deleting("user-1", document["id"]) is True


def test_delete_row_removes_it(session_factory):
    document, _ = add()

    repository.delete_row(document["id"])

    assert repository.get_document_by_id(document["id"]) is None
    repository.delete_row(document["id"])  # deleting twice is fine
'''),
]


def main() -> int:
    root = Path.cwd()
    if not (root / "backend" / "ingestion").is_dir():
        print("Run this from the project root (backend\\ingestion was not found).")
        return 1

    written = skipped = 0
    for relative, content in FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists() and target.stat().st_size > 0 and not FORCE:
            print(f"SKIP   {relative} (already has content)")
            skipped += 1
            continue

        target.write_text(content, encoding="utf-8", newline="\n")
        print(f"WRITE  {relative}")
        written += 1

    print(f"\nDone: {written} written, {skipped} skipped.")
    print("Next:  cd backend\\ingestion")
    print("       pip install pypdf python-docx pytest pytest-asyncio")
    print("       python -m pytest -v")
    return 0


if __name__ == "__main__":
    sys.exit(main())