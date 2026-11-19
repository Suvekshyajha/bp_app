import asyncio
import logging
import re
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from retriever_client import retrieve, store
from llm import stream_response

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

    # One dedicated DB connection for this whole chat session (this WebSocket).
    # The Session runs on top of it. Both are closed in `finally` below.
    conn = engine.connect()
    db: Session = SessionLocal(bind=conn)

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

        # 5. Receive chat messages
        while True:

            try:
                data = await websocket.receive_json()

            except ValueError:
                await websocket.send_json({
                    "error": "Invalid JSON."
                })
                continue

            if not isinstance(data, dict):
                await websocket.send_json({
                    "error": "Message must be a JSON object."
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
                await websocket.send_json({
                    "error": "A valid query is required."
                })
                continue

            query = query.strip()

            # Which conversation is this message for? (one per "New chat")
            requested_id = data.get("conversation_id")

            if requested_id is None:
                conversation_id = default_conversation_id
            elif (
                isinstance(requested_id, str)
                and CONVERSATION_ID_PATTERN.match(requested_id)
            ):
                conversation_id = requested_id
            else:
                await websocket.send_json({
                    "error": "Invalid conversation id."
                })
                continue

            try:
                conversation = get_or_create_conversation(
                    db,
                    user_id,
                    conversation_id,
                    query,
                )

                # Close the read transaction before the slow Qdrant/LLM calls.
                # (If the conversation was just created it is already committed.)
                db.rollback()

                if conversation is None:
                    await websocket.send_json({
                        "error": "Conversation not found."
                    })
                    continue

                # 6. Retrieve relevant past messages from Qdrant
                results = await asyncio.to_thread(
                    retrieve,
                    query,
                    user_id,
                    5,
                )

                # 7. Save user message in SQLite
                user_message = Message(
                    id=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    role="user",
                    content=query,
                )

                db.add(user_message)
                db.commit()

                # 8. Store user message in Qdrant
                await asyncio.to_thread(
                    store,
                    user_id,
                    conversation_id,
                    "user",
                    query,
                )

                # 9. Generate the AI response, sending it token by token
                parts: list[str] = []

                async for token in stream_response(query, results):
                    parts.append(token)

                    await websocket.send_json({
                        "type": "token",
                        "content": token,
                    })

                answer = "".join(parts)

                # 10. Save assistant response in SQLite
                assistant_message = Message(
                    id=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    role="assistant",
                    content=answer,
                )

                db.add(assistant_message)
                db.commit()

                # 11. Store assistant response in Qdrant
                await asyncio.to_thread(
                    store,
                    user_id,
                    conversation_id,
                    "assistant",
                    answer,
                )

                # 12. Tell the frontend the answer is complete
                await websocket.send_json({
                    "type": "done",
                    "query": query,
                    "answer": answer,
                    "conversation_id": conversation_id,
                    "results": results,
                })

            except Exception:
                db.rollback()

                logger.exception("Chat request failed")

                await websocket.send_json({
                    "error": "Something went wrong. Please try again."
                })

    except WebSocketDisconnect:
        logger.info("WebSocket connection closed.")

    except Exception:
        logger.exception("WebSocket connection failed.")

    finally:
        db.close()
        conn.close()