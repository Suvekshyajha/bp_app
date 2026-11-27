"""Who is calling, and for which user (plan 5).

Ingestion is an internal service. The orchestrator proves itself with a shared
secret (`authorization: Bearer <INGEST_SERVICE_TOKEN>` in the call metadata)
and says which user it acts for in `x-user-id`. Ingestion trusts that entry
only after the token checked out, which is why the port must stay on an
internal network.
"""

import hmac
import logging
import os

import grpc
from dotenv import load_dotenv

from common.service_auth import require_service_token

load_dotenv()

logger = logging.getLogger(__name__)

MAX_USER_ID_CHARS = 200

_token: str | None = None
_ready = False


class Refused(Exception):
    """A call that is turned down: a gRPC status and a message for the user."""

    def __init__(self, code: grpc.StatusCode, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def init_from_env() -> None:
    """Read the token settings. Raises RuntimeError if the service must not start."""

    global _token, _ready

    _token = require_service_token(
        os.getenv("INGEST_SERVICE_TOKEN"),
        allow_no_auth=os.getenv("INGEST_ALLOW_NO_AUTH", "").strip().lower() == "true",
        bind_host=os.getenv("INGEST_BIND_HOST", "127.0.0.1"),
        name="INGEST_SERVICE_TOKEN",
    )
    _ready = True


def _check_token(authorization: str) -> None:
    if _token is None:
        return  # development mode, allowed only on this machine

    presented = ""
    if authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()

    if not hmac.compare_digest(presented.encode(), _token.encode()):
        raise Refused(grpc.StatusCode.UNAUTHENTICATED, "Invalid or missing service token.")


def require_user(metadata: dict) -> str:
    """Check the token first, then return the user id of the call."""

    if not _ready:
        raise Refused(grpc.StatusCode.UNAVAILABLE, "Service is starting.")

    _check_token(metadata.get("authorization", ""))

    user_id = metadata.get("x-user-id", "").strip()
    if not user_id or len(user_id) > MAX_USER_ID_CHARS:
        raise Refused(grpc.StatusCode.PERMISSION_DENIED, "Missing or invalid x-user-id.")

    return user_id
