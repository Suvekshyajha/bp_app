import asyncio
import logging
import math
import re
import uuid

import grpc

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

import chat_client
from chat_client import ChatServiceError
from rate_limit import chat_limiter

from database import SessionLocal, engine
from models import User
from auth import (
    ALLOWED_ORIGINS,
    SESSION_COOKIE_NAME,
    extend_session,
    get_session_user_id,
)


logger = logging.getLogger(__name__)

router = APIRouter()

# Custom close codes the frontend understands
WS_AUTH_FAILED = 4401      # no valid session: log in again
WS_ORIGIN_BLOCKED = 4403   # request came from a website we do not allow

# A conversation id sent by the frontend (a UUID like "3f2a...-...")
CONVERSATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{8,64}$")


# Most answers one browser connection may have in progress at once
MAX_ACTIVE_TURNS = 3


class ChatTurn:
    """One question being answered. Lets the receive loop cancel it."""

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.streaming = False  # cancel only applies while tokens are flowing

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


# What the browser is told when the chat service reports a problem
SERVICE_ERROR_MESSAGES = {
    grpc.StatusCode.NOT_FOUND: "Conversation not found.",
    grpc.StatusCode.RESOURCE_EXHAUSTED: (
        "The assistant is busy right now. Please try again in a moment."
    ),
    grpc.StatusCode.DEADLINE_EXCEEDED: (
        "The assistant took too long to answer. Please try again."
    ),
        grpc.StatusCode.ABORTED: (
        "This message is already being answered. Please wait a moment."
    ),
    grpc.StatusCode.UNAVAILABLE: (
        "The chat service is unavailable. Please try again shortly."
    ),
}

GENERIC_ERROR = "Something went wrong. Please try again."


async def answer_question(
    send,
    turn: ChatTurn,
    user_id: str,
    conversation_id: str,
    query: str,
    user_message_id: str | None = None,
    regenerate: bool = False,
) -> None:
    """Ask the chat service and pass what it says on to the browser.

    The service does the real work: it searches, saves, calls the model and
    saves the answer. This function only relays its events.

    Runs as its own task, so the connection keeps reading client messages
    (like "cancel") while the answer is being written. Cancelling this task
    cancels the gRPC call, and the service stops writing.
    """

    results: list[dict] = []
    parts: list[str] = []
    final_answer: str | None = None
    cancelled = False

     # The idempotency key. Only passed when the browser sent one.
    extra = {"user_message_id": user_message_id} if user_message_id else {}

    if user_message_id and regenerate:
        extra["regenerate"] = True

    events = chat_client.chat(user_id, conversation_id, query, **extra)

    try:
        async for kind, value in events:

            if kind == "context":
                results = value
                turn.streaming = True  # from here on, "cancel" is allowed

            elif kind == "token":
                parts.append(value)
                await send({
                    "type": "token",
                    "content": value,
                    "conversation_id": conversation_id,
                })

            elif kind == "done":
                final_answer = value["answer"]

    except asyncio.CancelledError:
        if not turn.streaming:
            raise  # the connection closed before anything started

        # The user pressed stop. The service saves what was written.
        cancelled = True

    except ChatServiceError as error:
        logger.warning("Chat service error %s: %s", error.code.name, error.message)

        await send({
            "error": SERVICE_ERROR_MESSAGES.get(error.code, GENERIC_ERROR),
            "conversation_id": conversation_id,
        })
        return

    except Exception:
        logger.exception("Chat request failed")

        try:
            await send({"error": GENERIC_ERROR, "conversation_id": conversation_id})
        except Exception:
            pass  # the connection is gone

        return

    finally:
        turn.streaming = False
        await events.aclose()

    answer = final_answer if final_answer is not None else "".join(parts)

    await send({
        "type": "done",
        "query": query,
        "answer": answer,
        "conversation_id": conversation_id,
        "results": results,
        "cancelled": cancelled,
    })


@router.websocket("/ws/chat")
async def chat(websocket: WebSocket):

    # 1. Accept first so the browser can read our close code.
    #    (Closing before accept() makes the browser see a bare HTTP 403.)
    await websocket.accept()

    # 2. Check which website opened this connection.
    #    The session cookie is sent automatically by the browser, so without
    #    this check a hostile website could use the visitor's login.
    origin = websocket.headers.get("origin")

    if origin and origin.rstrip("/") not in ALLOWED_ORIGINS:
        logger.warning("WebSocket rejected: origin not allowed: %s", origin)
        await websocket.close(code=WS_ORIGIN_BLOCKED, reason="Origin not allowed")
        return

    # 3. Check the session cookie
    session_token = websocket.cookies.get(SESSION_COOKIE_NAME)

    # One dedicated DB connection for this chat session's login checks.
    # (Each answer uses its own session, see answer_question.)
    conn = engine.connect()
    db: Session = SessionLocal(bind=conn)

    # The reader and the answering task both send, so sends take turns
    send_lock = asyncio.Lock()

    async def send(payload: dict) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    # One answer in progress per conversation, keyed by conversation id
    turns: dict[str, ChatTurn] = {}

    try:
        user_id = get_session_user_id(db, session_token)

        if not user_id:
            logger.warning("WebSocket rejected: no valid session.")
            await websocket.close(code=WS_AUTH_FAILED, reason="Not logged in")
            return

        logger.info("WebSocket session verified for user: %s", user_id)

        user = db.query(User).filter(
            User.id == user_id
        ).first()

        if not user:
            logger.warning("WebSocket rejected: user does not exist.")
            await websocket.close(code=WS_AUTH_FAILED, reason="User not found")
            return

        # End the read transaction. The connection stays open for the whole
        # chat session, but no transaction (and no lock) stays open while
        # we sit waiting for the next message.
        db.rollback()

        # 4. Default conversation id (used only if the frontend sends none)
        default_conversation_id = str(uuid.uuid5(
            uuid.NAMESPACE_DNS,
            f"day2-chat-{user_id}"
        ))

        logger.info("WebSocket connected for user: %s", user_id)

        # 5. Receive messages. The loop never waits for an answer to finish,
        #    so "cancel" gets through while tokens are streaming.
        while True:

            try:
                data = await websocket.receive_json()

            except ValueError:
                await send({"error": "Invalid JSON."})
                continue

            if not isinstance(data, dict):
                await send({"error": "Message must be a JSON object."})
                continue

            # Heartbeat from the browser: answer it and wait for the next message.
            # It checks the session, so an expired login is closed (and its DB
            # connection freed), but it does NOT extend the session. Only real
            # chat messages count as activity.
            if data.get("type") == "ping":
                if get_session_user_id(db, session_token) != user_id:
                    await websocket.close(
                        code=WS_AUTH_FAILED,
                        reason="Session ended",
                    )
                    return

                db.rollback()
                await send({"type": "pong"})
                continue

            requested_id = data.get("conversation_id")

            # Which conversation is this message for? (one per "New chat")
            if requested_id is None:
                conversation_id = default_conversation_id
            elif (
                isinstance(requested_id, str)
                and CONVERSATION_ID_PATTERN.match(requested_id)
            ):
                conversation_id = requested_id
            else:
                await send({"error": "Invalid conversation id."})
                continue

            # Stop button: ends the answer being streamed for that conversation
            # and keeps what was written
            if data.get("type") == "cancel":
                target = turns.get(conversation_id)

                if target and target.running and target.streaming:
                    target.task.cancel()
                continue

            # Per-user rate limit. It counts this user's messages across all of
            # their open tabs, and runs before any database or AI work so a
            # flood of messages stays cheap. The error carries the conversation
            # id so the browser can tell which chat it belongs to.
            allowed, retry_after = chat_limiter.allow(user_id)

            if not allowed:
                wait = max(1, math.ceil(retry_after))

                logger.warning("Rate limit reached for user: %s", user_id)

                await send({
                    "error": (
                        "You are sending messages too quickly. "
                        f"Please wait {wait} seconds and try again."
                    ),
                    "conversation_id": conversation_id,
                    "retry_after": wait,
                })
                continue

            # Logging out (or an expired session) ends the chat immediately
            if get_session_user_id(db, session_token) != user_id:
                await websocket.close(
                    code=WS_AUTH_FAILED,
                    reason="Session ended",
                )
                return

            # The user is active: restart their 12 minutes. This commits, so it
            # also closes the transaction the session check above opened.
            extend_session(db, session_token)

            query = data.get("query")

            if not isinstance(query, str) or not query.strip():
                await send({
                    "error": "A valid query is required.",
                    "conversation_id": conversation_id,
                })
                continue

            query = query.strip()
            # The id the browser gave this question. If the chat already has
            # it, the chat service deletes it and everything after it first.
            user_message_id = data.get("user_message_id")
            regenerate = data.get("regenerate") is True

            if user_message_id is not None:
                try:
                    if not isinstance(user_message_id, str):
                        raise ValueError
                    user_message_id = str(uuid.UUID(user_message_id))
                except ValueError:
                    await send({
                        "error": "Invalid message id.",
                        "conversation_id": conversation_id,
                    })
                    continue
            # Forget answers that have finished
            for key in [k for k, t in turns.items() if not t.running]:
                del turns[key]

            # One answer at a time per conversation...
            if conversation_id in turns:
                await send({
                    "error": "A reply is already in progress in this chat.",
                    "conversation_id": conversation_id,
                })
                continue

            # ...but different conversations can answer at the same time
            if len(turns) >= MAX_ACTIVE_TURNS:
                await send({
                    "error": "Too many chats are answering at once. Wait for one to finish.",
                    "conversation_id": conversation_id,
                })
                continue

            turn = ChatTurn()
            turns[conversation_id] = turn
            turn.task = asyncio.create_task(
                answer_question(
                    send,
                    turn,
                    user_id,
                    conversation_id,
                    query,
                    user_message_id,
                    regenerate,
                )
            )

    except WebSocketDisconnect:
        logger.info("WebSocket connection closed.")

    except Exception:
        logger.exception("WebSocket connection failed.")

    finally:
        # Browser left: stop every answer still being written
        running = [t.task for t in turns.values() if t.running]

        for task in running:
            task.cancel()

        if running:
            await asyncio.gather(*running, return_exceptions=True)

        db.close()
        conn.close()