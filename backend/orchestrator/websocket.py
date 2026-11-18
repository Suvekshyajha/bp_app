import asyncio
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from retriever_client import retrieve, store
from llm import generate_response

from database import SessionLocal
from models import User, Conversation, Message
from auth import verify_access_token


logger = logging.getLogger(__name__)

router = APIRouter()

# Custom close code: the frontend treats it as "log in again"
WS_AUTH_FAILED = 4401


@router.websocket("/ws/chat")
async def chat(websocket: WebSocket):

    # 1. Accept first so the browser can read our close code.
    #    (Closing before accept() makes the browser see a bare HTTP 403.)
    await websocket.accept()

    # 2. Get and verify login token
    token = websocket.query_params.get("token")

    if not token:
        logger.warning("WebSocket rejected: token is missing.")
        await websocket.close(code=WS_AUTH_FAILED, reason="Token missing")
        return

    try:
        user_id = verify_access_token(token)
        logger.info("WebSocket JWT verified for user: %s", user_id)

    except Exception:
        logger.warning("WebSocket rejected: invalid or expired token.")
        await websocket.close(code=WS_AUTH_FAILED, reason="Invalid or expired token")
        return

    # 3. Check whether user exists
    db: Session = SessionLocal()

    try:
        user = db.query(User).filter(
            User.id == user_id
        ).first()

        if not user:
            logger.warning("WebSocket rejected: user does not exist.")
            await websocket.close(code=WS_AUTH_FAILED, reason="User not found")
            return

        # 4. Get or create the user's conversation
        conversation_id = str(uuid.uuid5(
            uuid.NAMESPACE_DNS,
            f"day2-chat-{user_id}"
        ))

        conversation = db.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        ).first()

        if not conversation:
            conversation = Conversation(
                id=conversation_id,
                user_id=user_id,
                title="My Chat",
            )

            db.add(conversation)
            db.commit()

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

            query = data.get("query")

            if not isinstance(query, str) or not query.strip():
                await websocket.send_json({
                    "error": "A valid query is required."
                })
                continue

            query = query.strip()

            try:
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

                # 9. Generate AI response
                answer = await asyncio.to_thread(
                    generate_response,
                    query,
                    results,
                )

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

                # 12. Send response to frontend
                await websocket.send_json({
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