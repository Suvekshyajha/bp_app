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


def _stubs():
    """Create the channel on first use (it needs a running event loop)."""

    global _channel, _chat_stub, _history_stub

    if _channel is None:
        _channel = grpc.aio.insecure_channel(CHAT_SERVICE_ADDRESS)
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
            }
            for message in response.messages
        ],
        "next_cursor": response.next_page_token or None,
    }


async def close() -> None:
    """Call on app shutdown."""

    global _channel, _chat_stub, _history_stub

    if _channel is not None:
        await _channel.close()

    _channel = _chat_stub = _history_stub = None
