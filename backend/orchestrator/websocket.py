import asyncio
import logging
import math
import re
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from retriever_client import retrieve, store, RetrieverError
from llm import stream_response
from rate_limit import chat_limiter

from database import SessionLocal, engine
from models import User, Conversation, Message
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


def get_or_create_conversation(
    db: Session,
    user_id: str,
    conversation_id: str,
    first_query: str,
) -> Conversation | None:
    """Return the user's conversation, creating it on first use.

    Returns None if the id already belongs to another user.
    """
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id
    ).first()

    if conversation:
        return conversation if conversation.user_id == user_id else None

    conversation = Conversation(
        id=conversation_id,
        user_id=user_id,
        title=" ".join(first_query.split())[:40],
    )

    db.add(conversation)
    db.commit()

    return conversation


# How much of THIS chat the model gets to see on each question
HISTORY_MAX_MESSAGES = 10
HISTORY_MAX_CHARS = 6000


def load_chat_history(db: Session, conversation_id: str) -> list[dict]:
    """The latest messages of one chat, oldest first, ready for the LLM.

    Call it BEFORE the new question is saved, so the question is not
    sent twice. The newest messages are kept first; older ones are dropped
    once the size budget is used up.
    """
    rows = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(HISTORY_MAX_MESSAGES)
        .all()
    )

    history: list[dict] = []
    used = 0

    for row in rows:  # newest first
        used += len(row.content)

        if history and used > HISTORY_MAX_CHARS:
            break

        history.append({"role": row.role, "content": row.content})

    history.reverse()  # oldest first, the order the chat was written in

    return history


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


async def answer_question(
    send,
    turn: ChatTurn,
    user_id: str,
    conversation_id: str,
    query: str,
) -> None:
    """Retrieve context, stream the answer, save everything.

    Runs as its own task, so the connection keeps reading client messages
    (like "cancel") while the answer is being written.
    """

    store_user_task: asyncio.Task | None = None

    # This task has its own DB session: the connection's session belongs to
    # the receive loop.
    with SessionLocal() as db:
        try:
            conversation = get_or_create_conversation(
                db,
                user_id,
                conversation_id,
                query,
            )

            # What was said earlier in THIS chat (so follow-ups make sense).
            # Read before the new question is saved below.
            history = (
                load_chat_history(db, conversation_id)
                if conversation is not None
                else []
            )

            # Close the read transaction before the slow retriever/LLM calls.
            db.rollback()

            if conversation is None:
                await send({"error": "Conversation not found.", "conversation_id": conversation_id})
                return

            # 6. Retrieve relevant past messages. They stream back over the
            #    gRPC stream and are collected here (best match first).
            #    Limited to this user AND this one chat.
            results = await retrieve(query, user_id, conversation_id, 5)

            # 7. Save the user message in SQLite
            db.add(Message(
                id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                role="user",
                content=query,
            ))
            db.commit()

            # 8. Store it in Qdrant in the background while the LLM runs.
            #    (After retrieval, so the question cannot retrieve itself.)
            store_user_task = asyncio.create_task(
                store(user_id, conversation_id, "user", query)
            )

            # 9. Stream the answer token by token
            parts: list[str] = []
            cancelled = False
            tokens = stream_response(query, results, history)
            turn.streaming = True

            try:
                async for token in tokens:
                    parts.append(token)
                    await send({
                        "type": "token",
                        "content": token,
                        "conversation_id": conversation_id,
                    })

            except asyncio.CancelledError:
                # The user pressed stop (or left). Keep what was written.
                cancelled = True

            finally:
                turn.streaming = False
                await tokens.aclose()

            answer = "".join(parts)

            # 10. Save the assistant response in SQLite
            if answer:
                db.add(Message(
                    id=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    role="assistant",
                    content=answer,
                ))
                db.commit()

            # 11. Store it in Qdrant. The answer is already saved and shown,
            #     so a failure here is logged and not shown as a chat error.
            try:
                await store_user_task
                if answer:
                    await store(user_id, conversation_id, "assistant", answer)
            except RetrieverError:
                logger.exception("Could not store messages in the retriever")

            # 12. Tell the frontend the answer is complete
            await send({
                "type": "done",
                "query": query,
                "answer": answer,
                "conversation_id": conversation_id,
                "results": results,
                "cancelled": cancelled,
            })

        except asyncio.CancelledError:
            # Connection closed before streaming started: nothing to send to
            if store_user_task:
                store_user_task.cancel()
            raise

        except Exception:
            db.rollback()

            if store_user_task:
                store_user_task.cancel()

            logger.exception("Chat request failed")

            try:
                await send({
                    "error": "Something went wrong. Please try again.",
                    "conversation_id": conversation_id,
                })
            except Exception:
                pass  # the connection is gone


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
                answer_question(send, turn, user_id, conversation_id, query)
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