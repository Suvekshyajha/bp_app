import asyncio
import hmac
import logging
import os
import uuid 
from contextlib import aclosing
from datetime import datetime, timezone
import grpc
import groq
from google.protobuf.timestamp_pb2 import Timestamp
import chat_pb2
import chat_pb2_grpc
import repository
from crypto import check_configured
from database import Base, engine, migrate_schema
from qdrant_store import create_collection, delete_conversation_points
from repository import ConversationNotFound, InvalidCursor
from service_auth import require_service_token
from turn import Context, Done, Token, TurnInProgress, run_turn


logger = logging.getLogger(__name__)

PORT = int(os.getenv("CHAT_SERVICE_PORT", "50051"))

# Which network interface to listen on. The default is this machine only.
# In Docker (or when the orchestrator runs on another machine) set it to
# 0.0.0.0 AND turn on TLS and the service token below.
BIND_HOST = os.getenv("CHAT_BIND_HOST", "127.0.0.1")

# Optional TLS (all three are file paths). Without CHAT_TLS_CERT the port is
# plain text, which is only acceptable on a trusted internal network.
TLS_CERT = os.getenv("CHAT_TLS_CERT")
TLS_KEY = os.getenv("CHAT_TLS_KEY")
# If set, callers must also present a certificate signed by this CA (mTLS)
TLS_CLIENT_CA = os.getenv("CHAT_TLS_CLIENT_CA")

# Shared secret the orchestrator sends with every call. The service trusts the
# user_id it is given, so only callers that know this secret may talk to it.
SERVICE_TOKEN = os.getenv("CHAT_SERVICE_TOKEN") or None

# Development only: start without a token (allowed only on this machine)
ALLOW_NO_AUTH = os.getenv("CHAT_ALLOW_NO_AUTH", "").lower() == "true"

# ---------- Converting between our data and protobuf ----------

ROLES = {
    "user": chat_pb2.ROLE_USER,
    "assistant": chat_pb2.ROLE_ASSISTANT,
}


def to_timestamp(value: datetime | str | None) -> Timestamp:
    """SQLite gives UTC times without a label; Qdrant payloads hold ISO text."""

    stamp = Timestamp()

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return stamp

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)

        stamp.FromDatetime(value)

    return stamp


def to_retrieved_message(item: dict) -> chat_pb2.RetrievedMessage:
    payload = item.get("payload") or {}

    return chat_pb2.RetrievedMessage(
        id=item["id"],
        similarity=item["score"],
        role=ROLES.get(payload.get("role"), chat_pb2.ROLE_UNSPECIFIED),
        content=payload.get("content", ""),
        created_at=to_timestamp(payload.get("timestamp")),
    )


# ---------- Who may call this service ----------

class ServiceTokenInterceptor(grpc.aio.ServerInterceptor):
    """Rejects every call that does not carry the shared secret.

    The secret travels in the `authorization` metadata ("Bearer <token>"),
    so it must go over TLS or a private network.
    """

    def __init__(self, token: str):
        self._expected = f"Bearer {token}".encode()

        async def deny(request, context):
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "Missing or wrong service token.",
            )

        self._deny = grpc.unary_unary_rpc_method_handler(deny)

    async def intercept_service(self, continuation, handler_call_details):
        metadata = dict(handler_call_details.invocation_metadata or ())
        given = metadata.get("authorization", "")

        if isinstance(given, str):
            given = given.encode()

        if not hmac.compare_digest(given, self._expected):
            return self._deny

        return await continuation(handler_call_details)


def read_file(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def add_port(server: grpc.aio.Server) -> None:
    address = f"{BIND_HOST}:{PORT}"

    if TLS_CERT and TLS_KEY:
        credentials = grpc.ssl_server_credentials(
            [(read_file(TLS_KEY), read_file(TLS_CERT))],
            root_certificates=read_file(TLS_CLIENT_CA) if TLS_CLIENT_CA else None,
            require_client_auth=bool(TLS_CLIENT_CA),
        )
        server.add_secure_port(address, credentials)
        logger.info("Listening on %s with TLS%s", address, " (mTLS)" if TLS_CLIENT_CA else "")
        return

    if TLS_CERT or TLS_KEY:
        raise RuntimeError("Set both CHAT_TLS_CERT and CHAT_TLS_KEY, or neither.")

    server.add_insecure_port(address)
    logger.warning("Listening on %s WITHOUT TLS", address)

    if BIND_HOST not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "This port is open to the network without encryption. "
            "Use TLS (CHAT_TLS_CERT / CHAT_TLS_KEY) or a private network."
        )


def saved_context(items: list[dict]) -> list[dict]:
    """Saved related messages, in the shape to_retrieved_message reads."""

    return [
        {
            "id": item.get("id", ""),
            "score": item.get("score", 0.0),
            "payload": {
                "role": item.get("role"),
                "content": item.get("content", ""),
                "timestamp": item.get("timestamp"),
            },
        }
        for item in items
        if isinstance(item, dict)
    ]


# ---------- Chat ----------

class ChatServicer(chat_pb2_grpc.ChatServiceServicer):

    async def Chat(self, request, context):
        if (
            not request.user_id
            or not request.conversation_id
            or not request.query.strip()
        ):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "user_id, conversation_id and query are required",
            )
                # The idempotency key: the id the browser chose for this question.
        # Same id + same text again = the saved answer is sent back.
        extra = {}

        if request.user_message_id:
            try:
                extra["user_message_id"] = str(uuid.UUID(request.user_message_id))
            except ValueError:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "user_message_id must be a UUID",
                )

        try:
            async with aclosing(
                run_turn(
                    request.user_id,
                    request.conversation_id,
                    request.query.strip(),
                    regenerate=request.regenerate,
                    **extra,
                )
            ) as events:
                async for event in events:
                    if isinstance(event, Context):
                        yield chat_pb2.ChatEvent(
                            context=chat_pb2.Context(
                                messages=[to_retrieved_message(r) for r in event.results]
                            )
                        )

                    elif isinstance(event, Token):
                        yield chat_pb2.ChatEvent(
                            token=chat_pb2.Token(text=event.text)
                        )

                    elif isinstance(event, Done):
                        yield chat_pb2.ChatEvent(
                            done=chat_pb2.Done(
                                user_message_id=event.user_message_id,
                                assistant_message_id=event.assistant_message_id,
                                answer=event.answer,
                            )
                        )

        except TurnInProgress:
            await context.abort(
                grpc.StatusCode.ABORTED,
                "This message is already being answered.",
            )

        except ConversationNotFound:
            await context.abort(grpc.StatusCode.NOT_FOUND, "Conversation not found.")

        except groq.RateLimitError:
            await context.abort(
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                "The language model is busy. Try again shortly.",
            )

        except groq.APITimeoutError:
            await context.abort(
                grpc.StatusCode.DEADLINE_EXCEEDED,
                "The language model took too long to answer.",
            )

        except groq.APIConnectionError:
            await context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "Could not reach the language model.",
            )

        except Exception:
            logger.exception("Chat turn failed")
            await context.abort(grpc.StatusCode.INTERNAL, "Internal error.")


# ---------- History ----------

class HistoryServicer(chat_pb2_grpc.HistoryServiceServicer):

    async def ListConversations(self, request, context):
        if not request.user_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "user_id is required")

        try:
            items, next_cursor = await asyncio.to_thread(
                repository.list_conversations,
                request.user_id,
                request.page_size or 20,
                request.page_token or None,
            )

        except InvalidCursor:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid cursor.")

        return chat_pb2.ListConversationsResponse(
            conversations=[
                chat_pb2.Conversation(
                    id=item["id"],
                    title=item["title"],
                    created_at=to_timestamp(item["created_at"]),
                    updated_at=to_timestamp(item["updated_at"]),
                )
                for item in items
            ],
            next_page_token=next_cursor or "",
        )

    async def ListMessages(self, request, context):
        if not request.user_id or not request.conversation_id:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "user_id and conversation_id are required",
            )

        try:
            items, next_cursor = await asyncio.to_thread(
                repository.list_messages,
                request.user_id,
                request.conversation_id,
                request.page_size or 30,
                request.page_token or None,
            )

        except ConversationNotFound:
            await context.abort(grpc.StatusCode.NOT_FOUND, "Conversation not found.")

        except InvalidCursor:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Invalid cursor.")

        return chat_pb2.ListMessagesResponse(
            messages=[
                chat_pb2.Message(
                    id=item["id"],
                    role=ROLES.get(item["role"], chat_pb2.ROLE_UNSPECIFIED),
                    content=item["content"],
                    created_at=to_timestamp(item["created_at"]),
                    context=[
                        to_retrieved_message(saved_item)
                        for saved_item in saved_context(item["context"])
                    ],
                )
                for item in items
            ],
            next_page_token=next_cursor or "",
        )

    async def DeleteConversation(self, request, context):
        if not request.user_id or not request.conversation_id:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "user_id and conversation_id are required",
            )

        try:
            owned = await asyncio.to_thread(
                repository.owns_conversation,
                request.user_id,
                request.conversation_id,
            )

            if not owned:
                raise ConversationNotFound(request.conversation_id)

            # Search index first. If it fails nothing is deleted and the user
            # can simply try again; the other order could leave a chat's text
            # in the index with no way to reach it.
            try:
                await asyncio.to_thread(
                    delete_conversation_points,
                    request.user_id,
                    request.conversation_id,
                )
            except Exception:
                logger.exception("Could not delete the chat from the search index")
                await context.abort(
                    grpc.StatusCode.UNAVAILABLE,
                    "Could not reach the search index.",
                )

            await asyncio.to_thread(
                repository.delete_conversation,
                request.user_id,
                request.conversation_id,
            )

        except ConversationNotFound:
            await context.abort(grpc.StatusCode.NOT_FOUND, "Conversation not found.")

        return chat_pb2.DeleteConversationResponse()


# ---------- Start up ----------

async def serve():
    # Refuse to start without a service token (before doing anything else)
    token = require_service_token(SERVICE_TOKEN, ALLOW_NO_AUTH, BIND_HOST)

    # Stop right away if the encryption key is missing, not on the first message
    check_configured()

    # Create the SQLite tables and the Qdrant collection if they are missing,
    # and add columns that older databases do not have yet
    Base.metadata.create_all(bind=engine)
    migrate_schema()
    await asyncio.to_thread(create_collection)

    if token:
        server = grpc.aio.server(interceptors=[ServiceTokenInterceptor(token)])
    else:
        server = grpc.aio.server()  # CHAT_ALLOW_NO_AUTH, local development


    chat_pb2_grpc.add_ChatServiceServicer_to_server(ChatServicer(), server)
    chat_pb2_grpc.add_HistoryServiceServicer_to_server(HistoryServicer(), server)

    add_port(server)
    await server.start()

    print(f"Chat service running on {BIND_HOST}:{PORT}")

    try:
        await server.wait_for_termination()
    except asyncio.CancelledError:
        await server.stop(grace=5)
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())