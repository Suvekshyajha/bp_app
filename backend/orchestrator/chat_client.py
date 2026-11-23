"""The orchestrator's connection to the chat service.

One gRPC channel, two stubs:
  - ChatService.Chat: server streaming. One call per question. Cancelling
    the call is how the stop button reaches the service.
  - HistoryService: plain request/response calls for the sidebar and the
    message list.
"""

import logging
import os
from datetime import timezone
from typing import AsyncIterator

import grpc
from dotenv import load_dotenv

import chat_pb2
import chat_pb2_grpc

load_dotenv()

logger = logging.getLogger(__name__)

CHAT_SERVICE_ADDRESS = os.getenv(
    "CHAT_SERVICE_ADDRESS",
    os.getenv("RETRIEVER_ADDRESS", "localhost:50051"),
)

# Shared secret the chat service requires on every call (CHAT_SERVICE_TOKEN
# there too). The service trusts the user_id we send, so this is what stops
# other programs on the network from asking for someone else's chats.
CHAT_SERVICE_TOKEN = os.getenv("CHAT_SERVICE_TOKEN") or None

# TLS. Set CHAT_TLS_CA to the certificate (file path) that signed the chat
# service's certificate and the connection is encrypted. For mutual TLS also
# set CHAT_TLS_CLIENT_CERT and CHAT_TLS_CLIENT_KEY.
CHAT_TLS_CA = os.getenv("CHAT_TLS_CA")
CHAT_TLS_CLIENT_CERT = os.getenv("CHAT_TLS_CLIENT_CERT")
CHAT_TLS_CLIENT_KEY = os.getenv("CHAT_TLS_CLIENT_KEY")

# A whole answer may take a while; history calls should be quick
CHAT_TIMEOUT_SECONDS = 120
HISTORY_TIMEOUT_SECONDS = 10


class ChatServiceError(Exception):
    """The chat service answered with an error (or could not be reached)."""

    def __init__(self, code: grpc.StatusCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_channel: grpc.aio.Channel | None = None
_chat_stub: chat_pb2_grpc.ChatServiceStub | None = None
_history_stub: chat_pb2_grpc.HistoryServiceStub | None = None


def _read(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _make_channel() -> grpc.aio.Channel:
    # Half a client certificate would silently turn mutual TLS off: refuse
    if bool(CHAT_TLS_CLIENT_CERT) != bool(CHAT_TLS_CLIENT_KEY):
        raise RuntimeError(
            "Set both CHAT_TLS_CLIENT_CERT and CHAT_TLS_CLIENT_KEY, or neither."
        )

    if CHAT_TLS_CA:
        client_cert = CHAT_TLS_CLIENT_CERT and CHAT_TLS_CLIENT_KEY

        credentials = grpc.ssl_channel_credentials(
            root_certificates=_read(CHAT_TLS_CA),
            private_key=_read(CHAT_TLS_CLIENT_KEY) if client_cert else None,
            certificate_chain=_read(CHAT_TLS_CLIENT_CERT) if client_cert else None,
        )

        return grpc.aio.secure_channel(CHAT_SERVICE_ADDRESS, credentials)

    logger.warning(
        "Talking to the chat service WITHOUT TLS (CHAT_TLS_CA is not set). "
        "Only do this on a private network."
    )

    if CHAT_SERVICE_TOKEN:
        logger.warning("The service token is being sent without encryption.")

    return grpc.aio.insecure_channel(CHAT_SERVICE_ADDRESS)


def _metadata() -> tuple:
    """Sent with every call."""

    if CHAT_SERVICE_TOKEN:
        return (("authorization", f"Bearer {CHAT_SERVICE_TOKEN}"),)

    return ()


def _stubs():
    """Create the channel on first use (it needs a running event loop)."""

    global _channel, _chat_stub, _history_stub

    if _channel is None:
        _channel = _make_channel()
        _chat_stub = chat_pb2_grpc.ChatServiceStub(_channel)
        _history_stub = chat_pb2_grpc.HistoryServiceStub(_channel)

    return _chat_stub, _history_stub


def _role_name(role: int) -> str:
    return {
        chat_pb2.ROLE_USER: "user",
        chat_pb2.ROLE_ASSISTANT: "assistant",
    }.get(role, "unknown")


def _iso(timestamp) -> str:
    return timestamp.ToDatetime(tzinfo=timezone.utc).isoformat()


def _result_for_frontend(message) -> dict:
    """The shape the frontend's ContextPanel already reads."""

    return {
        "id": message.id,
        "score": message.similarity,
        "payload": {
            "message_id": message.id,
            "role": _role_name(message.role),
            "content": message.content,
            "timestamp": _iso(message.created_at),
        },
    }


# ---------- Chat ----------

async def chat(
    user_id: str,
    conversation_id: str,
    query: str,
) -> AsyncIterator[tuple[str, object]]:
    """Ask one question and yield what comes back, as (kind, value) pairs:

        ("context", [result, ...])     once, before the first token
        ("token", "text")              many
        ("done", {"answer": ..., ...}) once, last

    Closing this generator (or cancelling the task reading it) cancels the
    gRPC call, and the service stops writing the answer.
    """

    chat_stub, _ = _stubs()

    call = chat_stub.Chat(
        chat_pb2.ChatRequest(
            user_id=user_id,
            conversation_id=conversation_id,
            query=query,
        ),
        timeout=CHAT_TIMEOUT_SECONDS,
        metadata=_metadata(),
    )

    try:
        async for event in call:
            kind = event.WhichOneof("event")

            if kind == "context":
                yield "context", [
                    _result_for_frontend(m) for m in event.context.messages
                ]

            elif kind == "token":
                yield "token", event.token.text

            elif kind == "done":
                yield "done", {
                    "answer": event.done.answer,
                    "user_message_id": event.done.user_message_id,
                    "assistant_message_id": event.done.assistant_message_id,
                }

    except grpc.aio.AioRpcError as error:
        raise ChatServiceError(error.code(), error.details() or "") from error

    finally:
        call.cancel()  # harmless if the call already finished


# ---------- History ----------

async def list_conversations(
    user_id: str,
    limit: int,
    cursor: str | None,
) -> dict:
    _, history_stub = _stubs()

    try:
        response = await history_stub.ListConversations(
            chat_pb2.ListConversationsRequest(
                user_id=user_id,
                page_size=limit,
                page_token=cursor or "",
            ),
            timeout=HISTORY_TIMEOUT_SECONDS,
            metadata=_metadata(),
        )
    except grpc.aio.AioRpcError as error:
        raise ChatServiceError(error.code(), error.details() or "") from error

    return {
        "items": [
            {
                "id": item.id,
                "title": item.title,
                "created_at": _iso(item.created_at),
                "updated_at": _iso(item.updated_at),
            }
            for item in response.conversations
        ],
        "next_cursor": response.next_page_token or None,
    }


async def list_messages(
    user_id: str,
    conversation_id: str,
    limit: int,
    cursor: str | None,
) -> dict:
    _, history_stub = _stubs()

    try:
        response = await history_stub.ListMessages(
            chat_pb2.ListMessagesRequest(
                user_id=user_id,
                conversation_id=conversation_id,
                page_size=limit,
                page_token=cursor or "",
            ),
            timeout=HISTORY_TIMEOUT_SECONDS,
            metadata=_metadata(),
        )
    except grpc.aio.AioRpcError as error:
        raise ChatServiceError(error.code(), error.details() or "") from error

    return {
        "items": [
            {
                "id": message.id,
                "role": _role_name(message.role),
                "content": message.content,
                "created_at": _iso(message.created_at),
                # The related past messages the answer was written with,
                # in the same shape the live "done" message uses
                "results": [_result_for_frontend(m) for m in message.context],
            }
            for message in response.messages
        ],
        "next_cursor": response.next_page_token or None,
    }


async def delete_conversation(user_id: str, conversation_id: str) -> None:
    """Delete a chat for good. Raises ChatServiceError (NOT_FOUND if the chat
    does not exist or is not this user's)."""

    _, history_stub = _stubs()

    try:
        await history_stub.DeleteConversation(
            chat_pb2.DeleteConversationRequest(
                user_id=user_id,
                conversation_id=conversation_id,
            ),
            timeout=HISTORY_TIMEOUT_SECONDS,
            metadata=_metadata(),
        )
    except grpc.aio.AioRpcError as error:
        raise ChatServiceError(error.code(), error.details() or "") from error


async def close() -> None:
    """Call on app shutdown."""

    global _channel, _chat_stub, _history_stub

    if _channel is not None:
        await _channel.close()

    _channel = _chat_stub = _history_stub = None