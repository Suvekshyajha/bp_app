import logging
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