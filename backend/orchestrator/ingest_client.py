"""The orchestrator's connection to the ingestion service (documents).

One gRPC channel (proto/ingest.proto). Every call carries the service token
(who we are) and `x-user-id` (which user we act for). The browser never
chooses that id: it comes from the session, see documents.py.

Anything that goes wrong is raised as IngestError(status, detail), where
`status` is the HTTP status the browser API uses. A service that cannot be
reached at all is status 503.
"""

import logging
import os

import grpc
from dotenv import load_dotenv

import ingest_pb2
import ingest_pb2_grpc
from common.log import get_request_id

load_dotenv()

logger = logging.getLogger(__name__)


def _address_from_url(url: str) -> str:
    """"http://host:8100/" -> "host:8100", for settings written for the old
    HTTP connection."""

    return url.split("://", 1)[-1].split("/", 1)[0]


# The older name INGEST_SERVICE_URL still works if the address is not set
_OLD_URL = os.getenv("INGEST_SERVICE_URL", "")
INGEST_SERVICE_ADDRESS = (
    os.getenv("INGEST_SERVICE_ADDRESS")
    or _address_from_url(_OLD_URL)
    or "localhost:8100"
)

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
# An old "https://" address keeps meaning an encrypted connection
USE_TLS = _OLD_URL.startswith("https://") and not os.getenv("INGEST_SERVICE_ADDRESS")

UPLOAD_TIMEOUT_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 10
UPLOAD_BLOCK = 256 * 1024  # one message of an upload (gRPC allows 4 MB)

UNAVAILABLE_DETAIL = "The document service is unavailable."

# The service's answers, as the HTTP statuses the browser API knows.
# Anything else is 500, which documents.py reports as our own problem.
HTTP_STATUS = {
    grpc.StatusCode.PERMISSION_DENIED: 400,
    grpc.StatusCode.UNAUTHENTICATED: 401,
    grpc.StatusCode.NOT_FOUND: 404,
    grpc.StatusCode.FAILED_PRECONDITION: 409,
    grpc.StatusCode.OUT_OF_RANGE: 413,
    grpc.StatusCode.INVALID_ARGUMENT: 415,
    grpc.StatusCode.UNAVAILABLE: 503,
    grpc.StatusCode.DEADLINE_EXCEEDED: 503,
}


class IngestError(Exception):
    """The ingestion service answered with an error (or could not be reached)."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(detail)
        self.status = status
        self.detail = detail


_channel: grpc.aio.Channel | None = None
_stub: ingest_pb2_grpc.DocumentServiceStub | None = None


def _make_channel() -> grpc.aio.Channel:
    if INGEST_TLS_CA:
        with open(INGEST_TLS_CA, "rb") as handle:
            credentials = grpc.ssl_channel_credentials(root_certificates=handle.read())

        return grpc.aio.secure_channel(INGEST_SERVICE_ADDRESS, credentials)

    if USE_TLS:
        return grpc.aio.secure_channel(INGEST_SERVICE_ADDRESS, grpc.ssl_channel_credentials())

    logger.warning(
        "Talking to the ingestion service WITHOUT TLS (INGEST_TLS_CA is not set). "
        "Only do this on a private network."
    )

    if INGEST_SERVICE_TOKEN:
        logger.warning("The ingestion service token is being sent without encryption.")

    return grpc.aio.insecure_channel(INGEST_SERVICE_ADDRESS)


def _get_stub() -> ingest_pb2_grpc.DocumentServiceStub:
    """Create the channel on first use (it belongs to the running event loop)."""

    global _channel, _stub

    if _stub is None:
        _channel = _make_channel()
        _stub = ingest_pb2_grpc.DocumentServiceStub(_channel)

    return _stub


def _metadata(user_id: str) -> tuple:
    # Without a user id the call would be too wide: refuse instead of guessing
    if not user_id:
        raise ValueError("user_id is required")

    entries = [("x-user-id", user_id), ("x-request-id", get_request_id())]

    if INGEST_SERVICE_TOKEN:
        entries.append(("authorization", f"Bearer {INGEST_SERVICE_TOKEN}"))

    return tuple(entries)


def _to_error(error: grpc.aio.AioRpcError) -> IngestError:
    code = error.code()
    status = HTTP_STATUS.get(code, 500)

    if status == 503:
        # The details of a connection failure name addresses: not passed on
        logger.warning("Ingestion service not reachable: %s", code.name)
        return IngestError(503, UNAVAILABLE_DETAIL)

    return IngestError(status, error.details() or "" if status != 500 else "")


async def _call(method: str, request, user_id: str, timeout: float):
    metadata = _metadata(user_id)

    try:
        return await getattr(_get_stub(), method)(request, metadata=metadata, timeout=timeout)
    except grpc.aio.AioRpcError as error:
        raise _to_error(error) from None


def _document(message: ingest_pb2.Document) -> dict:
    """The shape the browser has always received."""

    def optional(source, field):
        return getattr(source, field) if source.HasField(field) else None

    chunking = None

    if message.HasField("chunking"):
        summary = message.chunking
        chunking = {
            "strategy_used": optional(summary, "strategy_used"),
            "fallback_reason": summary.fallback_reason,
            "chunk_count": optional(summary, "chunk_count"),
        }

    return {
        "id": message.id,
        "filename": message.filename,
        "status": message.status,
        "stage": optional(message, "stage"),
        "error": optional(message, "error"),
        "chunk_count": message.chunk_count,
        "created_at": optional(message, "created_at"),
        "chunking": chunking,
    }


async def upload(user_id: str, filename: str, stream) -> tuple[int, dict]:
    """Send one file. `stream` is a file object (documents.py has already
    limited its size). Returns (status, body): 202 for a new document, 200 for
    one that already exists."""

    def pieces():
        yield ingest_pb2.UploadRequest(info=ingest_pb2.UploadInfo(filename=filename))

        while block := stream.read(UPLOAD_BLOCK):
            yield ingest_pb2.UploadRequest(chunk=block)

    answer = await _call("Upload", pieces(), user_id, UPLOAD_TIMEOUT_SECONDS)
    body = _document(answer.document)

    if not answer.existing:
        body = {key: body[key] for key in ("id", "filename", "status")}

    return 202 if answer.queued else 200, body


async def list_documents(user_id: str) -> list[dict]:
    answer = await _call(
        "ListDocuments", ingest_pb2.ListDocumentsRequest(), user_id, REQUEST_TIMEOUT_SECONDS
    )
    return [_document(message) for message in answer.documents]


async def get_document(user_id: str, document_id: str) -> dict:
    answer = await _call(
        "GetDocument",
        ingest_pb2.GetDocumentRequest(document_id=document_id),
        user_id,
        REQUEST_TIMEOUT_SECONDS,
    )
    return _document(answer)


async def delete_document(user_id: str, document_id: str) -> None:
    await _call(
        "DeleteDocument",
        ingest_pb2.DeleteDocumentRequest(document_id=document_id),
        user_id,
        REQUEST_TIMEOUT_SECONDS,
    )


async def close() -> None:
    """Call on app shutdown."""

    global _channel, _stub

    if _channel is not None:
        await _channel.close()

    _channel = None
    _stub = None
