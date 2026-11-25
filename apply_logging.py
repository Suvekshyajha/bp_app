#!/usr/bin/env python3
"""apply_logging.py - add request-id tracing and failure logging to the backend.

    python apply_logging.py PATH_TO_BACKEND              # apply
    python apply_logging.py PATH_TO_BACKEND --dry-run    # show the diff only
    python apply_logging.py PATH_TO_BACKEND --revert     # restore the backups

PATH_TO_BACKEND is the folder that holds orchestrator/, retriever/, ingestion/
and shared_common/.

What it does
  1. Writes shared_common/common/log.py (your setup_logging / set_request_id /
     get_request_id) if it is not there yet, and points every entry point at it.
  2. Request-id tracing: a middleware in the orchestrator and the ingestion API,
     the id sent on every call orchestrator -> ingestion (X-Request-ID header)
     and orchestrator -> retriever (x-request-id gRPC metadata), picked up again
     in the retriever's servicer methods; the worker uses the document id.
  3. Failure logging at the places that know the reason: every refusal in the
     upload endpoints, every gRPC abort in the retriever, the service-token
     checks, the downstream-error translators, the worker's expected failures
     (no_text, too large, ...), plus INFO lines for upload / delete / stages.

How it is safe
  - Functions are found by name with Python's AST, not by line number.
  - Every inserted line ends with "# [log]" and an edit is skipped when its line
    is already there, so running the script twice changes nothing.
  - A file is only written if the new text compiles. Originals are copied to
    <file>.bak-logging first (never overwritten), --revert restores them.
  - An edit whose anchor cannot be found is reported as SKIPPED, never guessed.
  - Line endings (CRLF / LF) and a BOM are kept as they were.
"""

import argparse
import ast
import difflib
import re
import sys
from pathlib import Path

MARK = "  # [log]"
BACKUP_SUFFIX = ".bak-logging"

# --------------------------------------------------------------------------
# Your log.py, as you wrote it. It is only written when the file is missing.
# --------------------------------------------------------------------------
LOG_PY = r'''import logging
import os
import sys
import uuid
from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar("request_id", default="-")
_service = "app"


class _Context(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.service = _service
        record.request_id = _request_id.get()
        return True


def setup_logging(service: str) -> None:
    """Call once in each entry point (main.py, worker.py)."""
    global _service
    _service = service

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(service)s] [%(request_id)s] %(name)s: %(message)s",
        "%H:%M:%S",
    ))
    handler.addFilter(_Context())

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        handlers=[handler],
        force=True,          # replaces any earlier basicConfig
    )


def set_request_id(value: str | None = None) -> str:
    """Start a trace (no argument) or continue one (pass the incoming ID)."""
    rid = value or uuid.uuid4().hex[:12]
    _request_id.set(rid)
    return rid


def get_request_id() -> str:
    return _request_id.get()
'''

MIDDLEWARE = '''# [trace-middleware] one id per request, one result line per request
@app.middleware("http")
async def trace_requests(request: Request, call_next):
    rid = set_request_id(request.headers.get("x-request-id"))
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("%s %s crashed", request.method, request.url.path)
        raise
    ms = int((time.monotonic() - started) * 1000)
    level = logging.WARNING if response.status_code >= 400 else logging.INFO
    logger.log(level, "%s %s -> %d (%d ms)",
               request.method, request.url.path, response.status_code, ms)
    response.headers["X-Request-ID"] = rid
    return response
'''

TRACE_HELPER = '''def _trace(context) -> None:
    """Continue the caller's trace: its request id arrives in the gRPC metadata."""
    set_request_id(dict(context.invocation_metadata() or ()).get("x-request-id"))
'''


# --------------------------------------------------------------------------
# One source file, edited in memory
# --------------------------------------------------------------------------
class SourceFile:
    def __init__(self, path: Path, report: list):
        self.path = path
        self.report = report
        raw = path.read_bytes()
        self.bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw[3:].decode("utf-8") if self.bom else raw.decode("utf-8")
        self.crlf = "\r\n" in text
        self.original = text.replace("\r\n", "\n")
        self.lines = self.original.split("\n")
        self._tree = None
        self.name = str(path)

    # ---- basics ----------------------------------------------------------
    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def tree(self) -> ast.Module:
        if self._tree is None:
            self._tree = ast.parse(self.text)
        return self._tree

    def note(self, status: str, what: str) -> None:
        self.report.append((status, self.path, what))

    def _insert(self, index: int, indent: int, texts: list[str]) -> None:
        self.lines[index:index] = [(" " * indent + t) if t else "" for t in texts]
        self._tree = None

    def func(self, qualname: str):
        scope = self.tree.body
        node = None
        for part in qualname.split("."):
            node = next(
                (
                    n for n in scope
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and n.name == part
                ),
                None,
            )
            if node is None:
                return None
            scope = node.body
        return node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None

    def _range_lines(self, node) -> list[str]:
        return [l.strip() for l in self.lines[node.lineno - 1 : node.end_lineno]]

    @staticmethod
    def _own_line(lines: list[str], node) -> bool:
        """True when the statement starts its own line (safe to insert before)."""
        return not lines[node.lineno - 1][: node.col_offset].strip()

    # ---- imports / logger ------------------------------------------------
    def _last_import_end(self) -> int:
        """Line index (0-based, exclusive end) after the last top-level import."""
        end = 0
        body = self.tree.body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
            end = body[0].end_lineno
        for node in body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                end = node.end_lineno
        return end

    def ensure_import(self, statement: str) -> None:
        """`import x` or `from m import a, b`: added, or merged into an existing line."""
        node = ast.parse(statement).body[0]

        if isinstance(node, ast.Import):
            wanted = {a.name for a in node.names}
            have = {a.name for n in self.tree.body if isinstance(n, ast.Import) for a in n.names}
            if wanted <= have:
                return
        else:
            wanted = {a.name for a in node.names}
            for n in self.tree.body:
                if isinstance(n, ast.ImportFrom) and n.module == node.module and n.level == 0:
                    have = {a.name for a in n.names}
                    if wanted <= have:
                        return
                    line = self.lines[n.lineno - 1]
                    if n.lineno == n.end_lineno and "(" not in line:
                        names = sorted(have | wanted, key=lambda s: (s.lower(), s))
                        self.lines[n.lineno - 1] = f"from {node.module} import {', '.join(names)}"
                        self._tree = None
                        return

        self._insert(self._last_import_end(), 0, [statement])

    def ensure_logger(self) -> None:
        for n in self.tree.body:
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "logger" for t in n.targets):
                return
        self.ensure_import("import logging")
        self._insert(self._last_import_end(), 0, ["", "logger = logging.getLogger(__name__)"])

    # ---- generic edits ---------------------------------------------------
    def insert_at_start(self, qualname: str, texts: list[str], what: str) -> None:
        f = self.func(qualname)
        if f is None:
            return self.note("SKIPPED", f"{what} (function {qualname} not found)")
        if all(t.strip() in self._range_lines(f) for t in texts):
            return self.note("already", what)

        first = f.body[0]
        is_doc = isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) and isinstance(first.value.value, str)
        if is_doc and len(f.body) == 1:
            index, indent = first.end_lineno, first.col_offset
        else:
            target = f.body[1] if is_doc else first
            index, indent = target.lineno - 1, target.col_offset
        self._insert(index, indent, texts)
        self.note("applied", what)

    def before_statements(self, qualname: str, predicate, build, what: str) -> None:
        f = self.func(qualname)
        if f is None:
            return self.note("SKIPPED", f"{what} (function {qualname} not found)")
        snapshot = self.text
        nodes = sorted(
            (n for n in ast.walk(f) if isinstance(n, ast.stmt) and predicate(n)),
            key=lambda n: n.lineno,
            reverse=True,
        )
        if not nodes:
            return self.note("SKIPPED", f"{what} (nothing to attach to in {qualname})")

        done = skipped = 0
        for n in nodes:
            if not self._own_line(self.lines, n):
                skipped += 1
                continue
            texts = build(n, snapshot)
            if self.lines[n.lineno - 2].strip() == texts[0].strip():
                continue
            self._insert(n.lineno - 1, n.col_offset, texts)
            done += 1

        extra = f", {skipped} on a shared line left alone" if skipped else ""
        self.note("applied" if done else "already", f"{what} ({done} places{extra})")

    def in_handler(self, qualname: str, exception_names: set[str], texts: list[str], what: str) -> None:
        f = self.func(qualname)
        if f is None:
            return self.note("SKIPPED", f"{what} (function {qualname} not found)")

        def names(t):
            if isinstance(t, ast.Tuple):
                return {e.id for e in t.elts if isinstance(e, ast.Name)}
            return {t.id} if isinstance(t, ast.Name) else set()

        handler = next(
            (n for n in ast.walk(f) if isinstance(n, ast.ExceptHandler) and n.type is not None and names(n.type) == exception_names),
            None,
        )
        if handler is None:
            return self.note("SKIPPED", f"{what} (handler for {sorted(exception_names)} not found)")
        first = handler.body[0]
        if self.lines[first.lineno - 2].strip() == texts[0].strip():
            return self.note("already", what)
        if not self._own_line(self.lines, first):
            return self.note("SKIPPED", f"{what} (handler body is on the same line)")
        self._insert(first.lineno - 1, first.col_offset, texts)
        self.note("applied", what)

    def near_line(self, qualname: str, pattern: str, texts: list[str], what: str, after: bool = False) -> None:
        f = self.func(qualname)
        if f is None:
            return self.note("SKIPPED", f"{what} (function {qualname} not found)")
        rx = re.compile(pattern)
        for i in range(f.lineno - 1, f.end_lineno):
            line = self.lines[i]
            if rx.search(line.strip()) and not line.strip().startswith("#"):
                indent = len(line) - len(line.lstrip())
                neighbour = self.lines[i + 1 if after else i - 1].strip()
                if neighbour == texts[0].strip():
                    return self.note("already", what)
                self._insert(i + 1 if after else i, indent, texts)
                return self.note("applied", what)
        self.note("SKIPPED", f"{what} (line /{pattern}/ not found in {qualname})")

    def replace_in_func(self, qualname: str, old: str, new: str, what: str) -> None:
        f = self.func(qualname)
        if f is None:
            return self.note("SKIPPED", f"{what} (function {qualname} not found)")
        start, end = f.lineno - 1, f.end_lineno
        block = "\n".join(self.lines[start:end])
        if new in block:
            return self.note("already", what)
        if old not in block:
            return self.note("SKIPPED", f"{what} (text not found in {qualname})")
        self.lines[start:end] = block.replace(old, new).split("\n")
        self._tree = None
        self.note("applied", what)

    def top_level_before(self, pattern: str, block: str, marker: str, what: str) -> None:
        if marker in self.text:
            return self.note("already", what)
        rx = re.compile(pattern)
        for n in self.tree.body:
            if rx.search(self.lines[n.lineno - 1]):
                self._insert(n.lineno - 1, 0, block.rstrip("\n").split("\n") + ["", ""])
                return self.note("applied", what)
        self.note("SKIPPED", f"{what} (line /{pattern}/ not found at top level)")

    # ---- the entry point wiring -------------------------------------------
    def wire_setup(self, service: str, log_module: str, local_in: str | None = None) -> None:
        what = f'setup_logging("{service}") from {log_module}'
        self.lines = self.text.replace("common.logging_setup", log_module).split("\n")
        self._tree = None
        call = f'setup_logging("{service}")'
        import_line = f"from {log_module} import setup_logging"

        if re.search(r"^\s*setup_logging\(", self.text, re.M):
            if local_in is None:
                self.ensure_import(import_line)
            return self.note("already", what)

        for i, line in enumerate(self.lines):
            m = re.match(r"^(\s*)logging\.basicConfig\(.*\)\s*$", line)
            if m:
                indent = m.group(1)
                new = ([f"{indent}{import_line}", "", f"{indent}{call}"] if local_in else [f"{indent}{call}"])
                self.lines[i : i + 1] = new
                self._tree = None
                if local_in is None:
                    self.ensure_import(import_line)
                return self.note("applied", f"{what} (replaced logging.basicConfig)")

        self.ensure_import(import_line)
        self.top_level_before(r"^app = FastAPI\(", "# Must run before the first request\n" + call, call, what)

    # ---- output ------------------------------------------------------------
    def result(self) -> str:
        return self.text


# --------------------------------------------------------------------------
# Small AST helpers for building log lines
# --------------------------------------------------------------------------
def _name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _name(node.value) + "." + node.attr
    return ""


def _is_http_exception_raise(n) -> bool:
    return isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call) and _name(n.exc.func) == "HTTPException"


def _is_context_abort(n) -> bool:
    return (
        isinstance(n, ast.Expr)
        and isinstance(n.value, ast.Await)
        and isinstance(n.value.value, ast.Call)
        and _name(n.value.value.func) == "context.abort"
    )


def _arg(call: ast.Call, keyword: str, position: int, source: str) -> str:
    for kw in call.keywords:
        if kw.arg == keyword:
            return ast.get_source_segment(source, kw.value) or "'?'"
    if len(call.args) > position:
        return ast.get_source_segment(source, call.args[position]) or "'?'"
    return "'?'"


def log_refusals(label: str, context: tuple = ()):
    """Before `raise HTTPException(...)`: a WARNING with the status and the reason.

    `context` is a list of (name, expression). Use `locals().get('x', '-')` for a
    variable that may not exist yet at that point, so the log line itself can
    never raise.
    """

    def build(node, source):
        call = node.exc
        fmt = f"{label} refused: status=%s detail=%s" + "".join(f" {k}=%s" for k, _ in context)
        args = [_arg(call, "status_code", 0, source), _arg(call, "detail", 1, source)] + [e for _, e in context]
        return [f'logger.warning("{fmt}", {", ".join(args)})' + MARK]

    return build


def log_aborts(label: str):
    """Before `await context.abort(code, message)`: a WARNING with both."""

    def build(node, source):
        call = node.value.value
        code = _arg(call, "code", 0, source)
        message = _arg(call, "details", 1, source)
        return [f'logger.warning("{label} refused: %s: %s", {code}.name, {message})' + MARK]

    return build


# --------------------------------------------------------------------------
# The plan: every edit, grouped by file
# --------------------------------------------------------------------------
def plan(root: Path, log_module: str):
    """Yield (relative path, function that edits a SourceFile)."""

    L = MARK  # shorthand

    # ---- orchestrator ----------------------------------------------------
    def orchestrator_main(sf):
        sf.wire_setup("orchestrator", log_module)
        for imp in ("import logging", "import time", "from fastapi import Request",
                    f"from {log_module} import set_request_id"):
            sf.ensure_import(imp)
        sf.ensure_logger()
        sf.top_level_before(r"^app\.(add_middleware|include_router)\(", MIDDLEWARE,
                            "[trace-middleware]", "request-id middleware (one line per request)")

    def orchestrator_ingest_client(sf):
        sf.ensure_import(f"from {log_module} import get_request_id")
        sf.replace_in_func(
            "_send", 'headers={"X-User-Id": user_id},',
            'headers={"X-User-Id": user_id, "X-Request-ID": get_request_id()},',
            "send X-Request-ID to the ingestion service")

    def orchestrator_chat_client(sf):
        sf.ensure_import(f"from {log_module} import get_request_id")
        sf.replace_in_func(
            "_metadata", 'return (("authorization", f"Bearer {CHAT_SERVICE_TOKEN}"),)',
            'return (("authorization", f"Bearer {CHAT_SERVICE_TOKEN}"), ("x-request-id", get_request_id()))',
            "send x-request-id to the retriever (with token)")
        sf.replace_in_func(
            "_metadata", "return ()", 'return (("x-request-id", get_request_id()),)',
            "send x-request-id to the retriever (no token)")

    def orchestrator_documents(sf):
        sf.ensure_logger()
        sf.before_statements(
            "upload_document", _is_http_exception_raise,
            log_refusals("Upload", (("user", "user.id"), ("file", "locals().get('full_name', '-')"),
                                    ("size", "locals().get('size', '-')"))),
            "why an upload was refused")
        sf.near_line(
            "upload_document", r"^return JSONResponse\(status_code=status_code, content=body\)",
            ['logger.info("Upload accepted: user=%s file=%s size=%s status=%s", user.id, full_name, size, status_code)' + L],
            "upload accepted (user, file, size)")
        sf.insert_at_start(
            "delete_document",
            ['logger.info("Delete requested: user=%s document=%s", user.id, document_id)' + L],
            "document delete requested")
        sf.insert_at_start(
            "to_http_error",
            ['logger.warning("Document service error: status=%s detail=%s", error.status, error.detail)' + L],
            "log what the ingestion service said")

    def orchestrator_history(sf):
        sf.ensure_logger()
        sf.insert_at_start(
            "to_http_error",
            ['logger.warning("Chat service error: %s %s", error.code.name, error.message)' + L],
            "log what the chat service said")
        sf.insert_at_start(
            "delete_conversation",
            ['logger.info("Delete conversation: user=%s conversation=%s", user.id, conversation_id)' + L],
            "conversation delete requested")

    def orchestrator_routes(sf):
        sf.ensure_logger()
        sf.near_line(
            "login", r"^set_session_cookie\(response, create_session\(db, user\.id\)\)",
            ['logger.info("Login: user=%s", user.id)' + L], "successful login")

    def orchestrator_websocket(sf):
        sf.ensure_import(f"from {log_module} import set_request_id")
        sf.ensure_import("import time")
        sf.ensure_logger()
        sf.insert_at_start(
            "answer_question",
            ["set_request_id()" + L,
             "_started = time.monotonic()" + L,
             'logger.info("Question started: conversation=%s documents=%d regenerate=%s", '
             'conversation_id, len(document_ids or ()), regenerate)' + L],
            "start of one question: new request id")
        sf.near_line(
            "answer_question", r'^answer = final_answer if final_answer is not None else "".join\(parts\)',
            ['logger.info("Question answered: conversation=%s cancelled=%s chars=%d ms=%d", '
             'conversation_id, cancelled, len(final_answer or "".join(parts)), '
             "int((time.monotonic() - _started) * 1000))" + L],
            "question answered (cancelled?, size, ms)", after=True)

    # ---- ingestion -------------------------------------------------------
    def ingestion_main(sf):
        sf.wire_setup("ingestion", log_module)
        for imp in ("import logging", "import time", "from fastapi import Request",
                    f"from {log_module} import set_request_id"):
            sf.ensure_import(imp)
        sf.ensure_logger()
        sf.top_level_before(r"^app\.(add_middleware|include_router)\(", MIDDLEWARE,
                            "[trace-middleware]", "request-id middleware (one line per request)")

    def ingestion_worker(sf):
        sf.wire_setup("ingestion-worker", log_module, local_in="main")
        sf.near_line(
            "run_once", r'^jobs\.process_document\(claimed\["id"\]\)',
            ['logging.getLogger("worker").info("Claimed document=%s", claimed["id"])' + L],
            "worker claimed a job")

    def ingestion_routes(sf):
        sf.ensure_logger()
        sf.before_statements(
            "upload", _is_http_exception_raise,
            log_refusals("Upload", (("user", "user_id"), ("file", "locals().get('full_name', '-')"),
                                    ("size", "locals().get('size', '-')"))),
            "why an upload was refused")
        sf.near_line(
            "upload", r"^return JSONResponse\(status_code=200, content=_public\(existing\)\)",
            ['logger.info("Upload duplicate: user=%s document=%s", user_id, document_id)' + L],
            "same file uploaded again")
        sf.near_line(
            "upload", r"^status_code = 202 if created else 200",
            ['logger.info("Upload stored: user=%s document=%s file=%s size=%d created=%s", '
             'user_id, document["id"], filename, size, created)' + L],
            "upload stored (document id links it to the job)")
        sf.insert_at_start(
            "delete_document",
            ['logger.info("Delete requested: user=%s document=%s", user_id, document_id)' + L],
            "document delete requested")

    def ingestion_auth(sf):
        sf.ensure_logger()
        sf.before_statements("_check_token", _is_http_exception_raise, log_refusals("Service token"),
                             "bad service token")
        sf.before_statements("require_user", _is_http_exception_raise, log_refusals("Request"),
                             "missing user id / service not ready")

    def ingestion_jobs(sf):
        sf.ensure_import(f"from {log_module} import set_request_id")
        sf.insert_at_start(
            "process_document", ["set_request_id(document_id[:12])" + L],
            "the job's trace id is the document id")
        f = sf.func("process_document")
        if f is not None:
            stages = sorted({
                m.group(1)
                for i in range(f.lineno - 1, f.end_lineno)
                for m in [re.search(r'repository\.set_stage\(document_id, "(\w+)"\)', sf.lines[i])]
                if m
            })
            for stage in stages:
                sf.near_line(
                    "process_document", rf'^repository\.set_stage\(document_id, "{stage}"\)',
                    [f'logger.info("stage={stage} document=%s", document_id)' + L],
                    f"job stage: {stage}", after=True)
        sf.in_handler(
            "process_document", {"ParseError", "ChunkingError"},
            ['logger.warning("Job rejected: document=%s code=%s", document_id, error.code)' + L],
            "expected failures (no_text, too large, ...) were never logged")

    # ---- retriever -------------------------------------------------------
    def retriever_main(sf):
        sf.wire_setup("retriever", log_module)
        sf.ensure_import(f"from {log_module} import set_request_id")
        sf.top_level_before(r"^class ServiceTokenInterceptor", TRACE_HELPER, "def _trace(context)",
                            "helper that reads x-request-id")
        for method, label in (("ChatServicer.Chat", "Chat"),
                              ("HistoryServicer.ListConversations", "ListConversations"),
                              ("HistoryServicer.ListMessages", "ListMessages"),
                              ("HistoryServicer.DeleteConversation", "DeleteConversation")):
            sf.insert_at_start(method, ["_trace(context)" + L], f"{label}: continue the caller's trace")
            sf.before_statements(method, _is_context_abort, log_aborts(label), f"{label}: why a call was refused")
        sf.near_line(
            "ServiceTokenInterceptor.intercept_service", r"^return self\._deny",
            ['logger.warning("Service call refused (bad token): method=%s", handler_call_details.method)' + L],
            "calls with a wrong service token")

    yield "shared_common/common/log.py", None  # handled separately
    yield "orchestrator/main.py", orchestrator_main
    yield "orchestrator/ingest_client.py", orchestrator_ingest_client
    yield "orchestrator/chat_client.py", orchestrator_chat_client
    yield "orchestrator/documents.py", orchestrator_documents
    yield "orchestrator/history.py", orchestrator_history
    yield "orchestrator/routes.py", orchestrator_routes
    yield "orchestrator/websocket.py", orchestrator_websocket
    yield "ingestion/main.py", ingestion_main
    yield "ingestion/worker.py", ingestion_worker
    yield "ingestion/routes.py", ingestion_routes
    yield "ingestion/auth.py", ingestion_auth
    yield "ingestion/jobs.py", ingestion_jobs
    yield "retriever/main.py", retriever_main


# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("backend", help="folder with orchestrator/, retriever/, ingestion/, shared_common/")
    parser.add_argument("--dry-run", action="store_true", help="show the diff, change nothing")
    parser.add_argument("--revert", action="store_true", help="restore every *.bak-logging backup")
    parser.add_argument("--log-module", default="common.log", help="import path of log.py (default: common.log)")
    args = parser.parse_args()

    root = Path(args.backend).resolve()
    if not (root / "shared_common").is_dir():
        print(f"error: {root} does not look like the backend folder (no shared_common/)", file=sys.stderr)
        return 2

    if args.revert:
        restored = 0
        for backup in root.rglob("*" + BACKUP_SUFFIX):
            backup.replace(backup.with_suffix(""))  # drops ".bak-logging"
            restored += 1
            print(f"restored {backup.with_suffix('').relative_to(root)}")
        print(f"{restored} file(s) restored. shared_common/common/log.py was left in place.")
        return 0

    report: list = []
    changed: dict[Path, tuple[str, str]] = {}

    for relative, editor in plan(root, args.log_module):
        path = root / relative

        if editor is None:  # log.py itself
            target = root / "shared_common" / "common" / "log.py"
            if target.exists():
                report.append(("already", target, "log.py exists, left as it is"))
            else:
                changed[target] = ("", LOG_PY)
                report.append(("applied", target, "created from your log.py"))
            continue

        if not path.exists():
            report.append(("SKIPPED", path, "file not found"))
            continue

        try:
            sf = SourceFile(path, report)
            editor(sf)
        except SyntaxError as error:
            report.append(("SKIPPED", path, f"could not parse before editing: {error}"))
            continue

        new_text = sf.result()
        if new_text == sf.original:
            continue
        try:
            compile(new_text, str(path), "exec")
        except SyntaxError as error:
            report.append(("SKIPPED", path, f"NOT WRITTEN, edited text did not compile: {error}"))
            continue
        changed[path] = (sf.original, new_text)

    # ---- report ---------------------------------------------------------
    width = max((len(str(p.relative_to(root))) for _, p, _ in report), default=10)
    for status, path, what in report:
        print(f"[{status:<7}] {str(path.relative_to(root)):<{width}}  {what}")

    # ---- write / diff -----------------------------------------------------
    for path, (before, after) in changed.items():
        if args.dry_run:
            diff = difflib.unified_diff(before.split("\n"), after.split("\n"),
                                        f"a/{path.relative_to(root)}", f"b/{path.relative_to(root)}", lineterm="")
            print("\n" + "\n".join(diff))
            continue

        if path.exists():
            backup = path.with_name(path.name + BACKUP_SUFFIX)
            if not backup.exists():
                backup.write_bytes(path.read_bytes())
            original = path.read_bytes()
            bom = original.startswith(b"\xef\xbb\xbf")
            crlf = b"\r\n" in original
        else:
            bom, crlf = False, False

        out = after.replace("\n", "\r\n") if crlf else after
        path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + out.encode("utf-8"))

    skipped = sum(1 for s, _, _ in report if s == "SKIPPED")
    applied = sum(1 for s, _, _ in report if s == "applied")
    print(f"\n{'would change' if args.dry_run else 'changed'} {len(changed)} file(s); "
          f"{applied} edit(s) applied, {skipped} skipped.")
    if not args.dry_run and changed:
        print("Next: pip install -e shared_common in the orchestrator's environment too, "
              "run uvicorn with --no-access-log, then run your tests.")
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())