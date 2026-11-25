"""The orchestrator's connection to the ingestion service (documents).

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
if not INGEST_SERVICE_TOKEN:
    raise RuntimeError(
        "INGEST_SERVICE_TOKEN is missing from orchestrator environment"
    )
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
