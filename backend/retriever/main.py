import asyncio
import logging
from datetime import datetime, timezone

import grpc
import groq
from google.protobuf.timestamp_pb2 import Timestamp

import chat_pb2
import chat_pb2_grpc

import repository
from database import Base, engine
from qdrant_store import create_collection
from repository import ConversationNotFound, InvalidCursor
from turn import Context, Done, Token, run_turn


logger = logging.getLogger(__name__)

PORT = 50051


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

        try:
            async for event in run_turn(
                request.user_id,
                request.conversation_id,
                request.query.strip(),
            ):
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
                )
                for item in items
            ],
            next_page_token=next_cursor or "",
        )


# ---------- Start up ----------

async def serve():
    # Create the SQLite tables and the Qdrant collection if they are missing
    Base.metadata.create_all(bind=engine)
    await asyncio.to_thread(create_collection)

    server = grpc.aio.server()

    chat_pb2_grpc.add_ChatServiceServicer_to_server(ChatServicer(), server)
    chat_pb2_grpc.add_HistoryServiceServicer_to_server(HistoryServicer(), server)

    server.add_insecure_port(f"[::]:{PORT}")
    await server.start()

    print(f"Chat service running on port {PORT}")

    try:
        await server.wait_for_termination()
    except asyncio.CancelledError:
        await server.stop(grace=5)
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
