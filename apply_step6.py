#!/usr/bin/env python3
"""Step 6 (orchestrator) of the Info Mate document-ingestion migration.

Usage (from the project root, the folder that contains `backend/`):

    python apply_step6.py              # apply the changes
    python apply_step6.py --check      # only report what is still missing

Changes (safe to run twice; every changed file gets a `.step6.bak` copy):

  ingest_client.py     NEW   httpx client to the ingestion service
  documents.py         NEW   POST/GET/DELETE /api/documents (session required)
  rate_limit.py              doc_upload_limiter (DOC_UPLOAD_RATE_LIMIT, 5 per 60 s)
  chat_client.py             chat(..., document_ids=()), citations in results
  websocket.py               document_ids validation + pass-through, 2 new error texts
  main.py                    includes the documents router, closes ingest_client
  .env.example               INGEST_* , MAX_UPLOAD_MB, DOC_UPLOAD_RATE_LIMIT
  requirements.txt           python-multipart (pinned)
  chat_pb2*.py               regenerated from backend/proto/chat.proto (grpcio-tools)
  tests/                     conftest.py (fake ingest_client, fake_chat takes
                             document_ids), test_documents.py, test_ingest_client.py,
                             test_chat_client_documents.py, new tests in test_Websocket.py

Afterwards run:   cd backend/orchestrator ; python -m pytest -q
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

CHECK_ONLY = "--check" in sys.argv


# ----------------------------------------------------------------- helpers

def find_orchestrator() -> Path:
    here = Path.cwd()
    for base in (here, here / "backend", here.parent, here.parent.parent):
        for path in (base / "backend" / "orchestrator", base / "orchestrator", base):
            if (path / "websocket.py").exists() and (path / "chat_client.py").exists():
                return path
    sys.exit("Could not find backend/orchestrator. Run this from the project root.")


class Source:
    """A text file edited with LF line endings; the original endings are kept."""

    def __init__(self, path: Path):
        self.path = path
        with open(path, encoding="utf-8", newline="") as handle:
            raw = handle.read()
        self.crlf = "\r\n" in raw
        self.text = raw.replace("\r\n", "\n")
        self.original = self.text

    def replace_once(self, old: str, new: str, label: str) -> None:
        count = self.text.count(old)
        if count != 1:
            sys.exit(f"{self.path.name}: cannot apply '{label}' ({count} matches for its anchor).")
        self.text = self.text.replace(old, new)

    def regex_once(self, pattern: str, new: str, label: str) -> None:
        self.text, count = re.subn(pattern, lambda _m: new, self.text, count=1, flags=re.S)
        if count != 1:
            sys.exit(f"{self.path.name}: cannot apply '{label}' (anchor not found).")

    def append(self, block: str) -> None:
        self.text = self.text.rstrip("\n") + "\n" + block

    def save(self) -> bool:
        if self.text == self.original:
            return False
        backup = self.path.with_name(self.path.name + ".step6.bak")
        if not backup.exists():
            shutil.copy2(self.path, backup)
        out = self.text.replace("\n", "\r\n") if self.crlf else self.text
        with open(self.path, "w", encoding="utf-8", newline="") as handle:
            handle.write(out)
        return True


def report(name: str, changed: bool, already: bool = False) -> None:
    print(f"  {'ok     ' if already else ('changed' if changed else 'same   ')}  {name}")


def write_new(path: Path, text: str, marker: str, label: str) -> None:
    """Write a new file. An existing empty file (or one without `marker`) is replaced."""
    if path.exists():
        current = path.read_text(encoding="utf-8", errors="ignore")
        if marker in current:
            report(label, False, already=True)
            return
        if current.strip():
            backup = path.with_name(path.name + ".step6.bak")
            if not backup.exists():
                shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    report(label + " (written)", True)


# ---------------------------------------------------------- ingest_client.py

INGEST_CLIENT = '''"""The orchestrator's connection to the ingestion service (documents).

One shared httpx client. Every call carries the service token (who we are) and
`X-User-Id` (which user we act for). The browser never chooses that id: it
comes from the session, see documents.py.

Anything that goes wrong is raised as IngestError(status, detail). A service
that cannot be reached at all is status 503.
"""

import logging
import os
import ssl

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

INGEST_SERVICE_URL = os.getenv("INGEST_SERVICE_URL", "http://localhost:8100").rstrip("/")

# Shared secret the ingestion service requires (INGEST_SERVICE_TOKEN there too).
# It trusts the user id we send, so this is what stops other programs on the
# network from asking for someone else's documents.
INGEST_SERVICE_TOKEN = os.getenv("INGEST_SERVICE_TOKEN") or None

# TLS: the certificate (file path) that signed the ingestion service's certificate
INGEST_TLS_CA = os.getenv("INGEST_TLS_CA") or None

UPLOAD_TIMEOUT_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 10


class IngestError(Exception):
    """The ingestion service answered with an error (or could not be reached)."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(detail)
        self.status = status
        self.detail = detail


_client: httpx.AsyncClient | None = None


def _make_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    headers = {}

    if INGEST_SERVICE_TOKEN:
        headers["Authorization"] = f"Bearer {INGEST_SERVICE_TOKEN}"

    verify: ssl.SSLContext | bool = True

    if INGEST_TLS_CA:
        verify = ssl.create_default_context(cafile=INGEST_TLS_CA)
    elif INGEST_SERVICE_URL.startswith("http://"):
        logger.warning(
            "Talking to the ingestion service WITHOUT TLS (INGEST_TLS_CA is not set). "
            "Only do this on a private network."
        )

        if INGEST_SERVICE_TOKEN:
            logger.warning("The ingestion service token is being sent without encryption.")

    return httpx.AsyncClient(
        base_url=INGEST_SERVICE_URL,
        headers=headers,
        verify=verify,
        transport=transport,
    )


def _get_client() -> httpx.AsyncClient:
    """Create the client on first use (it belongs to the running event loop)."""

    global _client

    if _client is None:
        _client = _make_client()

    return _client


def _detail_of(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return ""

    return detail if isinstance(detail, str) else ""


async def _send(method: str, path: str, user_id: str, timeout: float, **kwargs) -> httpx.Response:
    # Without a user id the call would be too wide: refuse instead of guessing
    if not user_id:
        raise ValueError("user_id is required")

    try:
        response = await _get_client().request(
            method,
            path,
            headers={"X-User-Id": user_id},
            timeout=timeout,
            **kwargs,
        )
    except httpx.HTTPError as error:  # refused, timed out, TLS problem ...
        logger.warning("Ingestion service not reachable: %s", type(error).__name__)
        raise IngestError(503, "The document service is unavailable.") from None

    if response.status_code >= 400:
        raise IngestError(response.status_code, _detail_of(response))

    return response


def _json(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        raise IngestError(502, "The document service sent an unreadable answer.") from None


async def upload(
    user_id: str,
    filename: str,
    stream,
    content_type: str = "application/octet-stream",
) -> tuple[int, dict]:
    """Send one file. `stream` is a file object (documents.py has already
    limited its size). Returns (status, body): 202 for a new document, 200 for
    one that already exists."""

    response = await _send(
        "POST",
        "/documents",
        user_id,
        UPLOAD_TIMEOUT_SECONDS,
        files={"file": (filename, stream, content_type)},
    )

    return response.status_code, _json(response)


async def list_documents(user_id: str) -> list[dict]:
    response = await _send("GET", "/documents", user_id, REQUEST_TIMEOUT_SECONDS)
    return _json(response)


async def get_document(user_id: str, document_id: str) -> dict:
    response = await _send("GET", f"/documents/{document_id}", user_id, REQUEST_TIMEOUT_SECONDS)
    return _json(response)


async def delete_document(user_id: str, document_id: str) -> None:
    await _send("DELETE", f"/documents/{document_id}", user_id, REQUEST_TIMEOUT_SECONDS)


async def close() -> None:
    """Call on app shutdown."""

    global _client

    if _client is not None:
        await _client.aclose()

    _client = None
'''


# -------------------------------------------------------------- documents.py

DOCUMENTS = '''"""Upload, list, look at and delete documents (REST, session cookie required).

The browser never says who it is: the user id is the session's user and is
what we send to the ingestion service. A `user_id` field in the form is
ignored. The service does its own checks too; these are the cheap early ones.
"""

import logging
import math
import os
import re
import tempfile
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile

import ingest_client
from auth import get_current_user
from ingest_client import IngestError
from models import User
from rate_limit import doc_upload_limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "10"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MULTIPART_OVERHEAD = 1024 * 1024  # headers and boundaries around the file
MAX_FILENAME_CHARS = 120
ALLOWED_EXTENSIONS = (".pdf", ".docx", ".txt", ".md")
READ_BLOCK = 1024 * 1024

# What the ingestion service said, passed on as it is. Anything else (a wrong
# service token, a crash) is OUR problem: it must never look like "you are
# logged out" to the browser, so it becomes 502.
PASS_THROUGH = {404, 409, 413, 415}


def to_http_error(error: IngestError) -> HTTPException:
    if error.status in PASS_THROUGH:
        return HTTPException(status_code=error.status, detail=error.detail or "Request refused.")

    if error.status == 503:
        return HTTPException(
            status_code=503,
            detail="The document service is unavailable. Please try again shortly.",
        )

    logger.error("Ingestion service answered %s", error.status)
    return HTTPException(
        status_code=502,
        detail="The document service could not handle the request.",
    )


def clean_filename(raw: str | None) -> str:
    """No folders and no control characters."""

    name = os.path.basename((raw or "").replace("\\\\", "/"))
    return re.sub(r"[\\x00-\\x1f\\x7f]", "", name).strip()


def checked_document_id(document_id: str) -> str:
    """A document id is a UUID. Anything else cannot exist, and is never
    placed into the URL we call."""

    try:
        return str(uuid.UUID(document_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="Document not found.") from None


@router.post("")
async def upload_document(request: Request, user: User = Depends(get_current_user)):
    # 1. The cheapest check first: a flood of uploads costs almost nothing
    allowed, retry_after = doc_upload_limiter.allow(user.id)

    if not allowed:
        wait = max(1, math.ceil(retry_after))
        raise HTTPException(
            status_code=429,
            detail=f"You are uploading too quickly. Please wait {wait} seconds and try again.",
            headers={"Retry-After": str(wait)},
        )

    # 2. Refuse a too-big body before reading any of it
    declared = request.headers.get("content-length")

    if not declared or not declared.isdigit():
        raise HTTPException(status_code=411, detail="Content-Length is required.")

    if int(declared) > MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD:
        raise HTTPException(status_code=413, detail=f"File is larger than {MAX_UPLOAD_MB} MB.")

    # 3. Read the form: one file, a few small fields (which we ignore)
    try:
        form = await request.form(max_files=1, max_fields=10)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Could not read the upload.") from None

    spool = tempfile.SpooledTemporaryFile(max_size=READ_BLOCK)

    try:
        upload = form.get("file")

        if not isinstance(upload, UploadFile):
            raise HTTPException(status_code=400, detail="A file is required.")

        full_name = clean_filename(upload.filename)

        if not full_name.lower().endswith(ALLOWED_EXTENSIONS):
            raise HTTPException(
                status_code=415,
                detail="Only .pdf, .docx, .txt and .md files are accepted.",
            )

        # 4. Count the bytes while copying; stop as soon as it is too big
        size = 0

        while True:
            block = await upload.read(READ_BLOCK)

            if not block:
                break

            size += len(block)

            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"File is larger than {MAX_UPLOAD_MB} MB.",
                )

            spool.write(block)

        if size == 0:
            raise HTTPException(status_code=415, detail="The file is empty.")

        spool.seek(0)

        # 5. The user id is the session's, never the form's
        try:
            status_code, body = await ingest_client.upload(
                user.id,
                full_name[:MAX_FILENAME_CHARS],
                spool,
            )
        except IngestError as error:
            raise to_http_error(error) from None

        return JSONResponse(status_code=status_code, content=body)

    finally:
        spool.close()
        await form.close()


@router.get("")
async def list_documents(user: User = Depends(get_current_user)):
    try:
        return await ingest_client.list_documents(user.id)
    except IngestError as error:
        raise to_http_error(error) from None


@router.get("/{document_id}")
async def get_document(document_id: str, user: User = Depends(get_current_user)):
    document_id = checked_document_id(document_id)

    try:
        return await ingest_client.get_document(user.id, document_id)
    except IngestError as error:
        raise to_http_error(error) from None


@router.delete("/{document_id}", status_code=204)
async def delete_document(document_id: str, user: User = Depends(get_current_user)):
    document_id = checked_document_id(document_id)

    try:
        await ingest_client.delete_document(user.id, document_id)
    except IngestError as error:
        raise to_http_error(error) from None

    return Response(status_code=204)
'''


RATE_LIMIT_ADD = '''

# Document uploads: this many per user in any window of this many seconds
DOC_UPLOAD_RATE_LIMIT = int(os.getenv("DOC_UPLOAD_RATE_LIMIT", "5"))
DOC_UPLOAD_RATE_LIMIT_WINDOW_SECONDS = float(
    os.getenv("DOC_UPLOAD_RATE_LIMIT_WINDOW_SECONDS", "60")
)

doc_upload_limiter = SlidingWindowRateLimiter(
    max_events=DOC_UPLOAD_RATE_LIMIT,
    window_seconds=DOC_UPLOAD_RATE_LIMIT_WINDOW_SECONDS,
)
'''

ENV_ADD = '''
# ---------- Documents (ingestion service) ----------
INGEST_SERVICE_URL=http://localhost:8100
# Must match INGEST_SERVICE_TOKEN of the ingestion service (backend/ingestion/.env).
# Make one with: python -c "import secrets; print(secrets.token_urlsafe(32))"
INGEST_SERVICE_TOKEN=change_me
# Certificate that signed the ingestion service's certificate. Leave empty on one machine.
INGEST_TLS_CA=
MAX_UPLOAD_MB=10
# Uploads per user in 60 seconds
DOC_UPLOAD_RATE_LIMIT=5
'''


# ------------------------------------------------------------ patch helpers

def patch_rate_limit(orch: Path) -> None:
    src = Source(orch / "rate_limit.py")
    if "doc_upload_limiter" in src.text:
        report("rate_limit.py", False, already=True)
        return
    src.append(RATE_LIMIT_ADD)
    report("rate_limit.py", src.save())


def patch_chat_client(orch: Path) -> None:
    src = Source(orch / "chat_client.py")
    if "document_ids" in src.text:
        report("chat_client.py", False, already=True)
        return

    src.regex_once(
        r"def _result_for_frontend\(message\) -> dict:.*?\n    \}\n",
        '''def _result_for_frontend(message) -> dict:
    """The shape the frontend's ContextPanel already reads."""

    payload = {
        "message_id": message.id,
        "role": _role_name(message.role),
        "content": message.content,
        "timestamp": _iso(message.created_at),
    }

    # Document excerpts also say where they came from. A past chat message has
    # an empty document_id and keeps exactly the shape it always had.
    if message.document_id:
        payload["document_id"] = message.document_id
        payload["filename"] = message.filename
        payload["chunk_index"] = message.chunk_index

        if message.section_title:
            payload["section_title"] = message.section_title

        if message.page:
            payload["page"] = message.page

    return {
        "id": message.id,
        "score": message.similarity,
        "payload": payload,
    }
''',
        "_result_for_frontend",
    )

    src.replace_once(
        "    regenerate: bool = False,\n) -> AsyncIterator[tuple[str, object]]:\n",
        "    regenerate: bool = False,\n    document_ids=(),\n) -> AsyncIterator[tuple[str, object]]:\n",
        "chat signature",
    )
    src.replace_once(
        "    `regenerate=True` asks for a new answer to the same question.\n",
        "    `regenerate=True` asks for a new answer to the same question.\n"
        "    `document_ids` are the documents to search besides the chat (the chat\n"
        "    service checks that they belong to this user).\n",
        "chat docstring",
    )
    src.replace_once(
        "            regenerate=regenerate,\n        ),\n        timeout=CHAT_TIMEOUT_SECONDS,",
        "            regenerate=regenerate,\n            document_ids=list(document_ids),\n        ),\n        timeout=CHAT_TIMEOUT_SECONDS,",
        "ChatRequest document_ids",
    )
    report("chat_client.py", src.save())


WS_HELPERS = '''
DOCUMENT_NOT_FOUND_MESSAGE = "A selected document was not found."

# Most documents one question may search (the chat service checks this too)
MAX_DOCUMENT_IDS = 5


def service_error_text(error: ChatServiceError) -> str:
    """What the browser is told. A missing DOCUMENT is not a missing chat: the
    chat service tells them apart by the text, which starts with "Document"."""

    if error.code == grpc.StatusCode.NOT_FOUND and error.message.startswith("Document"):
        return DOCUMENT_NOT_FOUND_MESSAGE

    return SERVICE_ERROR_MESSAGES.get(error.code, GENERIC_ERROR)


def parse_document_ids(raw) -> list[str] | None:
    """The `document_ids` of a browser message: a list of at most 5 different
    UUIDs. Returns them in one canonical form with duplicates removed (nothing
    sent gives an empty list), or None if anything is wrong."""

    if raw is None:
        return []

    # A cheap cap before looping, so a huge list costs nothing
    if not isinstance(raw, list) or len(raw) > 50:
        return None

    ids: list[str] = []

    for value in raw:
        if not isinstance(value, str):
            return None

        try:
            canonical = str(uuid.UUID(value))
        except ValueError:
            return None

        if canonical not in ids:
            ids.append(canonical)

    return ids if len(ids) <= MAX_DOCUMENT_IDS else None
'''


def patch_websocket(orch: Path) -> None:
    src = Source(orch / "websocket.py")
    if "parse_document_ids" in src.text:
        report("websocket.py", False, already=True)
        return

    src.replace_once(
        '    grpc.StatusCode.UNAVAILABLE: (\n        "The chat service is unavailable. Please try again shortly."\n    ),\n}\n',
        '    grpc.StatusCode.UNAVAILABLE: (\n        "The chat service is unavailable. Please try again shortly."\n    ),\n'
        '    grpc.StatusCode.FAILED_PRECONDITION: (\n'
        '        "A selected document is still being processed. Try again in a moment."\n'
        '    ),\n}\n',
        "FAILED_PRECONDITION text",
    )

    src.replace_once(
        'GENERIC_ERROR = "Something went wrong. Please try again."\n',
        'GENERIC_ERROR = "Something went wrong. Please try again."\n' + WS_HELPERS,
        "helpers",
    )

    src.replace_once(
        "    regenerate: bool = False,\n) -> None:\n",
        "    regenerate: bool = False,\n    document_ids: list[str] = (),\n) -> None:\n",
        "answer_question signature",
    )

    src.replace_once(
        '    if user_message_id and regenerate:\n        extra["regenerate"] = True\n',
        '    if user_message_id and regenerate:\n        extra["regenerate"] = True\n\n'
        '    # Only passed when documents were chosen\n'
        '    if document_ids:\n        extra["document_ids"] = list(document_ids)\n',
        "extra document_ids",
    )

    src.replace_once(
        '            "error": SERVICE_ERROR_MESSAGES.get(error.code, GENERIC_ERROR),\n',
        '            "error": service_error_text(error),\n',
        "service_error_text use",
    )

    src.replace_once(
        "            # Forget answers that have finished\n",
        '            # Documents to search besides the chat (max 5, each a UUID)\n'
        '            document_ids = parse_document_ids(data.get("document_ids"))\n\n'
        '            if document_ids is None:\n'
        '                await send({\n'
        '                    "error": "Invalid document id.",\n'
        '                    "conversation_id": conversation_id,\n'
        '                })\n'
        '                continue\n\n'
        "            # Forget answers that have finished\n",
        "validate document_ids",
    )

    src.replace_once(
        "                    user_message_id,\n                    regenerate,\n                )\n            )\n",
        "                    user_message_id,\n                    regenerate,\n                    document_ids,\n                )\n            )\n",
        "create_task document_ids",
    )
    report("websocket.py", src.save())


def patch_main(orch: Path) -> None:
    src = Source(orch / "main.py")
    if "documents" in src.text:
        report("main.py", False, already=True)
        return

    src.replace_once(
        "import chat_client\nimport routes\nimport websocket\n",
        "import chat_client\nimport documents\nimport ingest_client\nimport routes\nimport websocket\n",
        "imports",
    )
    src.replace_once(
        "    await chat_client.close()\n",
        "    await chat_client.close()\n    # ...and the HTTP client to the ingestion service\n    await ingest_client.close()\n",
        "lifespan close",
    )
    src.replace_once(
        "# WebSocket: /ws/chat\n",
        "# Documents: /api/documents ...\napp.include_router(documents.router, prefix=\"/api\")\n\n# WebSocket: /ws/chat\n",
        "include router",
    )
    report("main.py", src.save())


def patch_env_example(orch: Path) -> None:
    src = Source(orch / ".env.example")
    if "INGEST_SERVICE_URL" in src.text:
        report(".env.example", False, already=True)
        return
    src.append(ENV_ADD)
    report(".env.example", src.save())


def patch_requirements(orch: Path) -> None:
    src = Source(orch / "requirements.txt")
    if re.search(r"^python-multipart==", src.text, flags=re.M | re.I):
        report("requirements.txt", False, already=True)
        return

    def installed() -> str | None:
        probe = subprocess.run(
            [sys.executable, "-c",
             "import importlib.metadata as m; print(m.version('python-multipart'))"],
            capture_output=True, text=True,
        )
        return probe.stdout.strip() if probe.returncode == 0 and probe.stdout.strip() else None

    version = installed()
    if version is None:
        print("  installing python-multipart so it can be pinned ...")
        subprocess.run([sys.executable, "-m", "pip", "install", "python-multipart"],
                       capture_output=True, text=True)
        version = installed()
    if version is None:
        print("  WARNING  could not install python-multipart; add 'python-multipart==<version>' to requirements.txt yourself")
        return

    line = f"python-multipart=={version}\n"
    # keep the list sorted like the rest of the file: after python-dotenv
    if "python-dotenv==" in src.text:
        src.text = re.sub(r"(python-dotenv==[^\n]*\n)", lambda m: m.group(1) + line, src.text, count=1)
    else:
        src.text = src.text.rstrip("\n") + "\n" + line
    report("requirements.txt", src.save())


def regenerate_pb2(orch: Path) -> None:
    proto_dir = orch.parent / "proto"
    if not (proto_dir / "chat.proto").exists():
        print("  skipped  chat_pb2.py (backend/proto/chat.proto not found)")
        return

    probe = subprocess.run(
        [sys.executable, "-c",
         "import chat_pb2; raise SystemExit(0 if 'document_ids' in chat_pb2.ChatRequest.DESCRIPTOR.fields_by_name else 1)"],
        cwd=orch, capture_output=True, text=True,
    )
    if probe.returncode == 0:
        report("chat_pb2.py", False, already=True)
        return

    for name in ("chat_pb2.py", "chat_pb2_grpc.py"):
        path = orch / name
        backup = path.with_name(name + ".step6.bak")
        if path.exists() and not backup.exists():
            shutil.copy2(path, backup)

    result = subprocess.run(
        [sys.executable, "-m", "grpc_tools.protoc", f"-I{proto_dir}",
         f"--python_out={orch}", f"--grpc_python_out={orch}", str(proto_dir / "chat.proto")],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.exit("Could not regenerate chat_pb2.py. Run:  pip install grpcio-tools\n" + result.stderr)
    report("chat_pb2.py + chat_pb2_grpc.py (regenerated)", True)


# -------------------------------------------------------------------- tests

CONFTEST_INGEST = '''
# ---- Fake ingestion client (also installed before any import) ----

fake_ingest_client = types.ModuleType("ingest_client")


class IngestError(Exception):
    def __init__(self, status, detail=""):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class FakeIngestService:
    """Scripted behaviour for the fake ingestion service. Tests change these fields."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.error = None      # an IngestError to raise on every call
        self.calls = []        # (name, user_id, ...) of every call
        self.uploads = []      # {"user_id", "filename", "content"} of every upload
        self.upload_result = None   # (status, body); None = a normal 202
        self.documents = []    # what list_documents returns
        self.document = {"id": "x", "filename": "a.txt", "status": "ready"}


ingest_service = FakeIngestService()


async def fake_upload(user_id, filename, stream, content_type="application/octet-stream"):
    ingest_service.calls.append(("upload", user_id, filename))

    if ingest_service.error:
        raise ingest_service.error

    ingest_service.uploads.append(
        {"user_id": user_id, "filename": filename, "content": stream.read()}
    )

    return ingest_service.upload_result or (
        202,
        {"id": "11111111-1111-4111-8111-111111111111", "filename": filename, "status": "pending"},
    )


async def fake_list_documents(user_id):
    ingest_service.calls.append(("list", user_id))

    if ingest_service.error:
        raise ingest_service.error

    return ingest_service.documents


async def fake_get_document(user_id, document_id):
    ingest_service.calls.append(("get", user_id, document_id))

    if ingest_service.error:
        raise ingest_service.error

    return ingest_service.document


async def fake_delete_document(user_id, document_id):
    ingest_service.calls.append(("delete", user_id, document_id))

    if ingest_service.error:
        raise ingest_service.error


fake_ingest_client.IngestError = IngestError
fake_ingest_client.upload = fake_upload
fake_ingest_client.list_documents = fake_list_documents
fake_ingest_client.get_document = fake_get_document
fake_ingest_client.delete_document = fake_delete_document
fake_ingest_client.close = fake_close
sys.modules["ingest_client"] = fake_ingest_client
'''


def patch_conftest(tests: Path) -> None:
    src = Source(tests / "conftest.py")
    if "FakeIngestService" in src.text:
        report("tests/conftest.py", False, already=True)
        return

    src.replace_once(
        "        self.calls = []              # (user_id, conversation_id, query)\n",
        "        self.calls = []              # (user_id, conversation_id, query)\n"
        "        self.chat_options = []       # user_message_id, regenerate, document_ids of each chat call\n",
        "FakeChatService.reset",
    )

    src.replace_once(
        "async def fake_chat(user_id, conversation_id, query):\n"
        "    service.calls.append((user_id, conversation_id, query))\n",
        "async def fake_chat(\n"
        "    user_id,\n"
        "    conversation_id,\n"
        "    query,\n"
        "    user_message_id=None,\n"
        "    regenerate=False,\n"
        "    document_ids=(),\n"
        "):\n"
        "    service.calls.append((user_id, conversation_id, query))\n"
        "    service.chat_options.append({\n"
        '        "user_message_id": user_message_id,\n'
        '        "regenerate": regenerate,\n'
        '        "document_ids": list(document_ids),\n'
        "    })\n",
        "fake_chat",
    )

    src.replace_once(
        'sys.modules["chat_client"] = fake_chat_client\n',
        'sys.modules["chat_client"] = fake_chat_client\n' + CONFTEST_INGEST,
        "fake ingest_client",
    )

    src.replace_once("import auth  # noqa: E402\n", "import auth  # noqa: E402\nimport documents  # noqa: E402\n", "import documents")

    src.replace_once(
        "    application.include_router(history.router, prefix=\"/api\")\n",
        "    application.include_router(history.router, prefix=\"/api\")\n"
        "    application.include_router(documents.router, prefix=\"/api\")\n",
        "app fixture",
    )

    src.replace_once(
        "@pytest.fixture\ndef engine(tmp_path):",
        "@pytest.fixture(autouse=True)\n"
        "def fake_ingest():\n"
        '    """A clean scripted ingestion service for every test."""\n'
        "    ingest_service.reset()\n"
        "    return ingest_service\n\n\n"
        "@pytest.fixture\ndef engine(tmp_path):",
        "fake_ingest fixture",
    )
    report("tests/conftest.py", src.save())


TEST_DOCUMENTS = '''"""The document routes: session required, limits, filename cleaning, rate limit,
error mapping, and the user id always comes from the session."""

import pytest

import documents
from conftest import IngestError, login, make_user
from rate_limit import SlidingWindowRateLimiter

DOC = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def generous_limiter(monkeypatch):
    """Default: a limit so high it never gets in the way."""
    monkeypatch.setattr(documents, "doc_upload_limiter", SlidingWindowRateLimiter(1000, 60))


def upload(client, name="notes.txt", content=b"hello", **form):
    return client.post(
        "/api/documents",
        files={"file": (name, content, "application/octet-stream")},
        data=form,
    )


# ---------- a session is required ----------

@pytest.mark.parametrize("method, path", [
    ("post", "/api/documents"),
    ("get", "/api/documents"),
    ("get", f"/api/documents/{DOC}"),
    ("delete", f"/api/documents/{DOC}"),
])
def test_every_route_needs_a_session(client, fake_ingest, method, path):
    kwargs = {"files": {"file": ("a.txt", b"x", "text/plain")}} if method == "post" else {}

    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401
    assert fake_ingest.calls == []


# ---------- upload ----------

def test_upload_is_forwarded_for_the_session_user(logged_in, user, fake_ingest):
    response = upload(logged_in)

    assert response.status_code == 202
    assert response.json() == {"id": DOC, "filename": "notes.txt", "status": "pending"}
    assert fake_ingest.uploads == [
        {"user_id": user.id, "filename": "notes.txt", "content": b"hello"}
    ]


def test_an_existing_document_is_passed_on_as_200(logged_in, fake_ingest):
    fake_ingest.upload_result = (200, {"id": DOC, "filename": "notes.txt", "status": "ready"})

    response = upload(logged_in)

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_the_user_id_comes_from_the_session_even_if_the_form_sends_another(logged_in, user, fake_ingest):
    response = upload(logged_in, user_id="mallory", x_user_id="mallory")

    assert response.status_code == 202
    assert [u["user_id"] for u in fake_ingest.uploads] == [user.id]


def test_the_filename_loses_its_folders(logged_in, fake_ingest):
    upload(logged_in, name="..\\\\..\\\\secret/dir/report.txt")

    assert fake_ingest.uploads[0]["filename"] == "report.txt"


def test_clean_filename_drops_folders_and_control_characters():
    assert documents.clean_filename("..\\\\..\\\\secret/dir/re\\x01port\\x7f.txt") == "report.txt"
    assert documents.clean_filename("  spaced.md  ") == "spaced.md"
    assert documents.clean_filename(None) == ""


def test_a_long_filename_is_cut_to_120_characters(logged_in, fake_ingest):
    upload(logged_in, name="a" * 300 + ".txt")

    assert len(fake_ingest.uploads[0]["filename"]) == documents.MAX_FILENAME_CHARS


@pytest.mark.parametrize("name", ["virus.exe", "noextension", "photo.png", "archive.pdf.exe", ".txt.bak"])
def test_other_file_types_are_refused_and_never_forwarded(logged_in, fake_ingest, name):
    response = upload(logged_in, name=name)

    assert response.status_code == 415
    assert fake_ingest.calls == []


@pytest.mark.parametrize("name", ["a.pdf", "a.DOCX", "a.txt", "a.md"])
def test_the_four_allowed_types_are_accepted(logged_in, name):
    assert upload(logged_in, name=name).status_code == 202


def test_an_empty_file_is_refused(logged_in, fake_ingest):
    response = upload(logged_in, content=b"")

    assert response.status_code == 415
    assert fake_ingest.calls == []


def test_a_body_that_is_too_big_is_refused_from_its_content_length(logged_in, fake_ingest, monkeypatch):
    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(documents, "MULTIPART_OVERHEAD", 0)

    response = upload(logged_in, content=b"x" * 5000)

    assert response.status_code == 413
    assert fake_ingest.calls == []


def test_a_file_that_is_too_big_is_refused_while_counting_bytes(logged_in, fake_ingest, monkeypatch):
    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(documents, "MULTIPART_OVERHEAD", 100_000)  # the early check passes

    response = upload(logged_in, content=b"x" * 11)

    assert response.status_code == 413
    assert fake_ingest.calls == []


def test_a_file_exactly_at_the_limit_is_accepted(logged_in, monkeypatch):
    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(documents, "MULTIPART_OVERHEAD", 100_000)

    assert upload(logged_in, content=b"x" * 10).status_code == 202


def test_a_request_without_a_file_is_a_bad_request(logged_in, fake_ingest):
    response = logged_in.post("/api/documents", data={"note": "no file here"})

    assert response.status_code == 400
    assert fake_ingest.calls == []


def test_two_files_in_one_request_are_refused(logged_in, fake_ingest):
    response = logged_in.post(
        "/api/documents",
        files=[
            ("file", ("a.txt", b"one", "text/plain")),
            ("file", ("b.txt", b"two", "text/plain")),
        ],
    )

    assert response.status_code == 400
    assert fake_ingest.calls == []


# ---------- rate limit ----------

def test_uploads_over_the_limit_get_429_with_retry_after(logged_in, fake_ingest, monkeypatch):
    monkeypatch.setattr(documents, "doc_upload_limiter", SlidingWindowRateLimiter(1, 60))

    assert upload(logged_in).status_code == 202
    blocked = upload(logged_in)

    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert len(fake_ingest.uploads) == 1  # the blocked one never reached ingestion


def test_the_upload_limit_is_per_user(client, db, fake_ingest, monkeypatch):
    monkeypatch.setattr(documents, "doc_upload_limiter", SlidingWindowRateLimiter(1, 60))
    alice, bob = make_user(db, "alice"), make_user(db, "bob")

    login(client, db, alice)
    assert upload(client).status_code == 202
    assert upload(client).status_code == 429

    login(client, db, bob)
    assert upload(client).status_code == 202


# ---------- error mapping ----------

@pytest.mark.parametrize("ingest_status, expected", [
    (404, 404),
    (409, 409),
    (413, 413),
    (415, 415),
    (503, 503),
    (401, 502),  # a wrong service token is our fault: never a logout for the browser
    (400, 502),
    (500, 502),
])
def test_ingestion_errors_are_mapped(logged_in, fake_ingest, ingest_status, expected):
    fake_ingest.error = IngestError(ingest_status, "from ingestion")

    assert upload(logged_in).status_code == expected
    assert logged_in.get("/api/documents").status_code == expected
    assert logged_in.get(f"/api/documents/{DOC}").status_code == expected
    assert logged_in.delete(f"/api/documents/{DOC}").status_code == expected


def test_an_ingestion_message_for_the_user_is_passed_on(logged_in, fake_ingest):
    fake_ingest.error = IngestError(409, "You can keep at most 20 documents. Delete one first.")

    response = upload(logged_in)

    assert response.json()["detail"] == "You can keep at most 20 documents. Delete one first."


def test_an_unreachable_service_does_not_leak_details(logged_in, fake_ingest):
    fake_ingest.error = IngestError(503, "connect ECONNREFUSED 10.0.0.7:8100")

    response = logged_in.get("/api/documents")

    assert response.status_code == 503
    assert "10.0.0.7" not in response.text


# ---------- list, get, delete ----------

def test_list_is_forwarded_for_the_session_user(logged_in, user, fake_ingest):
    fake_ingest.documents = [{"id": DOC, "filename": "a.txt", "status": "ready"}]

    response = logged_in.get("/api/documents")

    assert response.json() == fake_ingest.documents
    assert fake_ingest.calls == [("list", user.id)]


def test_get_is_forwarded_for_the_session_user(logged_in, user, fake_ingest):
    response = logged_in.get(f"/api/documents/{DOC}")

    assert response.status_code == 200
    assert fake_ingest.calls == [("get", user.id, DOC)]


def test_delete_is_forwarded_and_gives_204(logged_in, user, fake_ingest):
    response = logged_in.delete(f"/api/documents/{DOC}")

    assert response.status_code == 204
    assert fake_ingest.calls == [("delete", user.id, DOC)]


@pytest.mark.parametrize("bad", ["not-a-uuid", "123", "..%2F..%2Fadmin", "11111111-1111-4111-8111-11111111111g"])
def test_an_id_that_is_not_a_uuid_is_not_found_and_never_forwarded(logged_in, fake_ingest, bad):
    assert logged_in.get(f"/api/documents/{bad}").status_code == 404
    assert logged_in.delete(f"/api/documents/{bad}").status_code == 404
    assert fake_ingest.calls == []


def test_an_uppercase_uuid_is_sent_in_its_canonical_form(logged_in, user, fake_ingest):
    logged_in.get(f"/api/documents/{DOC.upper()}")

    assert fake_ingest.calls == [("get", user.id, DOC)]
'''


TEST_INGEST_CLIENT = '''"""ingest_client against a fake HTTP service (httpx.MockTransport)."""

import importlib.util
import io
from pathlib import Path

import httpx
import pytest

# Other test files put a fake "ingest_client" into sys.modules. Load the real
# file under a private name so this test always checks the real code.
_spec = importlib.util.spec_from_file_location(
    "ingest_client_under_test",
    Path(__file__).resolve().parent.parent / "ingest_client.py",
)
real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real)

TOKEN = "s3cret-token"
DOC = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def service(monkeypatch):
    """Install a real client wired to `service.handler`. Records every request."""

    monkeypatch.setattr(real, "INGEST_SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(real, "INGEST_SERVICE_URL", "http://ingest.test")

    class Service:
        def __init__(self):
            self.requests = []
            self.handler = lambda request: httpx.Response(200, json=[])

        def __call__(self, request):
            self.requests.append(request)
            return self.handler(request)

    fake = Service()
    monkeypatch.setattr(real, "_client", real._make_client(transport=httpx.MockTransport(fake)))
    return fake


@pytest.mark.asyncio
async def test_every_call_carries_the_token_and_the_user_id(service):
    await real.list_documents("alice-id")

    request = service.requests[0]
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["x-user-id"] == "alice-id"
    assert (request.method, request.url.path) == ("GET", "/documents")


@pytest.mark.asyncio
async def test_upload_sends_the_file_as_multipart_and_returns_status_and_body(service):
    service.handler = lambda request: httpx.Response(202, json={"id": DOC, "status": "pending"})

    status, body = await real.upload("alice-id", "notes.txt", io.BytesIO(b"hello world"))

    request = service.requests[0]
    assert (status, body) == (202, {"id": DOC, "status": "pending"})
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b'filename="notes.txt"' in request.content
    assert b"hello world" in request.content
    assert request.headers["x-user-id"] == "alice-id"


@pytest.mark.asyncio
async def test_get_and_delete_use_the_document_path(service):
    service.handler = lambda request: (
        httpx.Response(204) if request.method == "DELETE" else httpx.Response(200, json={"id": DOC})
    )

    assert await real.get_document("alice-id", DOC) == {"id": DOC}
    assert await real.delete_document("alice-id", DOC) is None
    assert [(r.method, r.url.path) for r in service.requests] == [
        ("GET", f"/documents/{DOC}"),
        ("DELETE", f"/documents/{DOC}"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 404, 409, 413, 415, 500])
async def test_error_statuses_become_ingest_error_with_the_detail(service, status):
    service.handler = lambda request: httpx.Response(status, json={"detail": "why"})

    with pytest.raises(real.IngestError) as error:
        await real.get_document("alice-id", DOC)

    assert (error.value.status, error.value.detail) == (status, "why")


@pytest.mark.asyncio
async def test_an_error_without_json_has_an_empty_detail(service):
    service.handler = lambda request: httpx.Response(502, text="<html>bad gateway</html>")

    with pytest.raises(real.IngestError) as error:
        await real.list_documents("alice-id")

    assert (error.value.status, error.value.detail) == (502, "")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    httpx.ConnectError("refused"),
    httpx.ReadTimeout("slow"),
    httpx.ConnectTimeout("slow"),
])
async def test_an_unreachable_service_is_503_without_details(service, failure):
    def refuse(request):
        raise failure

    service.handler = refuse

    with pytest.raises(real.IngestError) as error:
        await real.list_documents("alice-id")

    assert error.value.status == 503
    assert "refused" not in error.value.detail


@pytest.mark.asyncio
async def test_an_unreadable_success_answer_is_502(service):
    service.handler = lambda request: httpx.Response(200, text="not json")

    with pytest.raises(real.IngestError) as error:
        await real.list_documents("alice-id")

    assert error.value.status == 502


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["", None])
async def test_a_call_without_a_user_id_is_refused_before_any_request(service, user):
    with pytest.raises(ValueError):
        await real.list_documents(user)

    assert service.requests == []


@pytest.mark.asyncio
async def test_close_drops_the_client(service):
    await real.close()

    assert real._client is None
'''


TEST_CHAT_CLIENT_DOCS = '''"""chat_client: document_ids reach the chat service and excerpts keep their source."""

import importlib.util
from pathlib import Path

import pytest

import chat_pb2

# Load the real chat_client under a private name (other tests install a fake).
_spec = importlib.util.spec_from_file_location(
    "chat_client_docs_under_test",
    Path(__file__).resolve().parent.parent / "chat_client.py",
)
real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real)

DOC = "11111111-1111-4111-8111-111111111111"


class FakeCall:
    def __init__(self, events):
        self._events = events
        self.cancelled = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self._events:
            yield event

    def cancel(self):
        self.cancelled = True


class FakeChatStub:
    def __init__(self, events):
        self.requests = []
        self._events = events

    def Chat(self, request, timeout=None, metadata=None):
        self.requests.append(request)
        return FakeCall(self._events)


def excerpt(**fields):
    base = dict(
        id="c1",
        similarity=0.8,
        role=chat_pb2.ROLE_UNSPECIFIED,
        content="Revenue was five.",
        document_id=DOC,
        filename="report.pdf",
        chunk_index=2,
        section_title="Results",
        page=3,
    )
    base.update(fields)
    return chat_pb2.RetrievedMessage(**base)


def past_message():
    return chat_pb2.RetrievedMessage(
        id="m1", similarity=0.7, role=chat_pb2.ROLE_USER, content="earlier"
    )


@pytest.fixture
def stub(monkeypatch):
    events = [
        chat_pb2.ChatEvent(context=chat_pb2.Context(messages=[past_message(), excerpt()])),
        chat_pb2.ChatEvent(token=chat_pb2.Token(text="Hi")),
        chat_pb2.ChatEvent(done=chat_pb2.Done(answer="Hi", user_message_id="u", assistant_message_id="a")),
    ]
    fake = FakeChatStub(events)
    monkeypatch.setattr(real, "_stubs", lambda: (fake, None))
    return fake


async def collect(**kwargs):
    return [item async for item in real.chat("alice-id", "chat-0001", "hello", **kwargs)]


@pytest.mark.asyncio
async def test_document_ids_are_sent_in_the_request(stub):
    await collect(document_ids=[DOC])

    assert list(stub.requests[0].document_ids) == [DOC]


@pytest.mark.asyncio
async def test_no_document_ids_by_default(stub):
    await collect()

    assert list(stub.requests[0].document_ids) == []


@pytest.mark.asyncio
async def test_an_excerpt_carries_its_source_in_the_payload(stub):
    events = await collect(document_ids=[DOC])
    _, results = events[0]
    payload = results[1]["payload"]

    assert payload["document_id"] == DOC
    assert payload["filename"] == "report.pdf"
    assert payload["chunk_index"] == 2
    assert payload["section_title"] == "Results"
    assert payload["page"] == 3
    assert payload["content"] == "Revenue was five."


@pytest.mark.asyncio
async def test_a_past_chat_message_keeps_exactly_its_old_shape(stub):
    events = await collect(document_ids=[DOC])
    _, results = events[0]

    assert set(results[0]["payload"]) == {"message_id", "role", "content", "timestamp"}


def test_empty_section_and_page_are_left_out_but_part_zero_stays():
    payload = real._result_for_frontend(excerpt(section_title="", page=0, chunk_index=0))["payload"]

    assert payload["chunk_index"] == 0
    assert "section_title" not in payload
    assert "page" not in payload
'''


TEST_WEBSOCKET_ADD = '''

# ---------- documents (step 6) ----------

DOC1 = "11111111-1111-4111-8111-111111111111"
DOC2 = "22222222-2222-4222-8222-222222222222"


def ask_with(ws, document_ids):
    message = {"query": "what was revenue", "conversation_id": CHAT}
    if document_ids is not _MISSING:
        message["document_ids"] = document_ids
    ws.send_json(message)
    return receive_until_done(ws)


_MISSING = object()


def test_document_ids_are_cleaned_and_passed_to_the_chat_service(logged_in, fake_service):
    with logged_in.websocket_connect("/ws/chat") as ws:
        received = ask_with(ws, [DOC1.upper(), DOC2, DOC1])

    assert received[-1]["type"] == "done"
    assert fake_service.chat_options[0]["document_ids"] == [DOC1, DOC2]  # canonical, no duplicates


def test_no_document_ids_means_none_are_sent(logged_in, fake_service):
    with logged_in.websocket_connect("/ws/chat") as ws:
        ask_with(ws, _MISSING)
        ask_with(ws, [])

    assert [o["document_ids"] for o in fake_service.chat_options] == [[], []]


def test_five_documents_are_allowed(logged_in, fake_service):
    ids = [f"{i}1111111-1111-4111-8111-111111111111" for i in range(5)]

    with logged_in.websocket_connect("/ws/chat") as ws:
        assert ask_with(ws, ids)[-1]["type"] == "done"

    assert fake_service.chat_options[0]["document_ids"] == ids


def test_duplicates_do_not_count_toward_the_limit(logged_in, fake_service):
    ids = [f"{i}1111111-1111-4111-8111-111111111111" for i in range(5)]

    with logged_in.websocket_connect("/ws/chat") as ws:
        assert ask_with(ws, ids + ids[:1])[-1]["type"] == "done"


@pytest.mark.parametrize("bad", [
    "not-a-list",
    {"id": DOC1},
    [123],
    [None],
    ["nope"],
    [DOC1, "nope"],
    [f"{i}1111111-1111-4111-8111-111111111111" for i in range(6)],  # six different ones
    [DOC1] * 51,
])
def test_invalid_document_ids_are_rejected_and_never_reach_the_chat_service(logged_in, fake_service, bad):
    with logged_in.websocket_connect("/ws/chat") as ws:
        received = ask_with(ws, bad)

    assert received == [{"error": "Invalid document id.", "conversation_id": CHAT}]
    assert fake_service.calls == []


def test_the_socket_stays_usable_after_an_invalid_document_id(logged_in, fake_service):
    with logged_in.websocket_connect("/ws/chat") as ws:
        ask_with(ws, ["nope"])
        assert ask_with(ws, [DOC1])[-1]["type"] == "done"


def test_a_document_still_processing_gets_its_own_message(logged_in, fake_service):
    fake_service.error = ChatServiceError(
        grpc.StatusCode.FAILED_PRECONDITION, "Document is still being processed."
    )

    with logged_in.websocket_connect("/ws/chat") as ws:
        received = ask_with(ws, [DOC1])

    assert received[-1]["error"] == "A selected document is still being processed. Try again in a moment."
    assert received[-1]["conversation_id"] == CHAT


def test_a_missing_document_is_not_reported_as_a_missing_chat(logged_in, fake_service):
    fake_service.error = ChatServiceError(grpc.StatusCode.NOT_FOUND, "Document not found.")

    with logged_in.websocket_connect("/ws/chat") as ws:
        received = ask_with(ws, [DOC1])

    assert received[-1]["error"] == "A selected document was not found."


def test_a_missing_chat_still_says_conversation_not_found(logged_in, fake_service):
    fake_service.error = ChatServiceError(grpc.StatusCode.NOT_FOUND, "Conversation not found.")

    with logged_in.websocket_connect("/ws/chat") as ws:
        received = ask_with(ws, [DOC1])

    assert received[-1]["error"] == "Conversation not found."
'''


def append_block(path: Path, marker: str, block: str, label: str, extra_import: str | None = None) -> None:
    src = Source(path)
    if marker in src.text:
        report(label, False, already=True)
        return
    # module-level import only (the file already imports it inside two tests)
    if extra_import and not re.search(rf"^from conftest import .*{extra_import}", src.text, flags=re.M):
        src.replace_once("from conftest import login\n", f"from conftest import {extra_import}, login\n", "import ChatServiceError")
    src.append(block)
    report(label, src.save())


# --------------------------------------------------------------------- main

def check(orch: Path) -> None:
    tests = orch / "tests"

    def text(path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""

    pb2 = subprocess.run(
        [sys.executable, "-c",
         "import chat_pb2; raise SystemExit(0 if 'document_ids' in chat_pb2.ChatRequest.DESCRIPTOR.fields_by_name else 1)"],
        cwd=orch, capture_output=True, text=True,
    ).returncode == 0

    checks = {
        "ingest_client.py written": "class IngestError" in text(orch / "ingest_client.py"),
        "documents.py written": "upload_document" in text(orch / "documents.py"),
        "rate_limit.py: doc_upload_limiter": "doc_upload_limiter" in text(orch / "rate_limit.py"),
        "chat_client.py: document_ids": "document_ids" in text(orch / "chat_client.py"),
        "websocket.py: document_ids validation": "parse_document_ids" in text(orch / "websocket.py"),
        "websocket.py: FAILED_PRECONDITION text": "FAILED_PRECONDITION" in text(orch / "websocket.py"),
        "main.py: documents router": "documents.router" in text(orch / "main.py"),
        "main.py: ingest_client closed": "ingest_client.close" in text(orch / "main.py"),
        ".env.example: INGEST_SERVICE_URL": "INGEST_SERVICE_URL" in text(orch / ".env.example"),
        "requirements.txt: python-multipart": "python-multipart" in text(orch / "requirements.txt"),
        "chat_pb2.py has document_ids": pb2,
        "tests/test_documents.py written": "test_every_route_needs_a_session" in text(tests / "test_documents.py"),
        "tests/test_ingest_client.py": (tests / "test_ingest_client.py").exists(),
        "tests/test_chat_client_documents.py": (tests / "test_chat_client_documents.py").exists(),
        "tests/conftest.py: fake ingest_client": "FakeIngestService" in text(tests / "conftest.py"),
        "tests/test_Websocket.py: document tests": "documents (step 6)" in text(tests / "test_Websocket.py"),
    }
    for name, ok in checks.items():
        print(f"  {'OK     ' if ok else 'MISSING'}  {name}")


def main() -> None:
    orch = find_orchestrator()
    tests = orch / "tests"
    print(f"Orchestrator folder: {orch}")

    if CHECK_ONLY:
        check(orch)
        return

    write_new(orch / "ingest_client.py", INGEST_CLIENT, "class IngestError", "ingest_client.py")
    write_new(orch / "documents.py", DOCUMENTS, "upload_document", "documents.py")
    patch_rate_limit(orch)
    patch_chat_client(orch)
    patch_websocket(orch)
    patch_main(orch)
    patch_env_example(orch)
    patch_requirements(orch)
    regenerate_pb2(orch)

    patch_conftest(tests)
    write_new(tests / "test_documents.py", TEST_DOCUMENTS, "test_every_route_needs_a_session", "tests/test_documents.py")
    write_new(tests / "test_ingest_client.py", TEST_INGEST_CLIENT, "ingest_client_under_test", "tests/test_ingest_client.py")
    write_new(tests / "test_chat_client_documents.py", TEST_CHAT_CLIENT_DOCS, "chat_client_docs_under_test", "tests/test_chat_client_documents.py")
    append_block(tests / "test_Websocket.py", "documents (step 6)", TEST_WEBSOCKET_ADD,
                 "tests/test_Websocket.py", extra_import="ChatServiceError")

    print("\nDone. Now run:  cd backend/orchestrator ; python -m pytest -q")


if __name__ == "__main__":
    main()