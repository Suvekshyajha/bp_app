"""Who is calling, and for which user (plan 5).

Ingestion is an internal service. The orchestrator proves itself with a shared
secret (`Authorization: Bearer <INGEST_SERVICE_TOKEN>`) and says which user it
acts for in `X-User-Id`. Ingestion trusts that header only after the token
checked out, which is why the port must stay on an internal network.
"""

import hmac
import os
from dotenv import load_dotenv


from fastapi import Header, HTTPException

load_dotenv()
from common.service_auth import require_service_token

MAX_USER_ID_CHARS = 200

_token: str | None = None
_ready = False


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


def _check_token(authorization: str | None) -> None:
    if _token is None:
        return  # development mode, allowed only on this machine

    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()

    if not hmac.compare_digest(presented.encode(), _token.encode()):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing service token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_user(
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> str:
    """FastAPI dependency: checks the token first, then returns the user id."""

    if not _ready:
        raise HTTPException(status_code=503, detail="Service is starting.")

    _check_token(authorization)

    user_id = (x_user_id or "").strip()
    if not user_id or len(user_id) > MAX_USER_ID_CHARS:
        raise HTTPException(status_code=400, detail="Missing or invalid X-User-Id header.")

    return user_id
