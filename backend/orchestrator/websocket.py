
import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from retriever_client import retrieve, store
from llm import generate_response

from database import SessionLocal
from models import User, Conversation, Message
from auth import verify_access_token

import uuid


logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/chat")
async def chat(websocket: WebSocket):

    # 1. Get and verify login token
    token = websocket.query_params.get("token")

    if not token:
        await websocket.close(code=1008)
        return

    try:
        user_id = verify_access_token(token)
    except Exception:
        await websocket.close(code=1008)
        return

    # 2. Check whether user exists
    db: Session = SessionLocal()

    try:
        user = db.query(User).filter(
            User.id == user_id
        ).first()

        if not user:
            await websocket.close(code=1008)
            return

        # 3. Get or create the user's conversation
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

        await websocket.accept()

        # 4. Receive chat messages
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
                # 5. Retrieve relevant past messages from Qdrant
                results = await asyncio.to_thread(
                    retrieve,
                    query,
                    user_id,
                    5,
                )

                # 6. Save user message in SQLite
                user_message = Message(
                    id=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    role="user",
                    content=query,
                )

                db.add(user_message)
                db.commit()

                # 7. Store user message in Qdrant
                await asyncio.to_thread(
                    store,
                    user_id,
                    conversation_id,
                    "user",
                    query,
                )

                # 8. Generate AI response
                answer = await asyncio.to_thread(
                    generate_response,
                    query,
                    results,
                )

                # 9. Save assistant response in SQLite
                assistant_message = Message(
                    id=str(uuid.uuid4()),
                    conversation_id=conversation_id,
                    role="assistant",
                    content=answer,
                )

                db.add(assistant_message)
                db.commit()

                # 10. Store assistant response in Qdrant
                await asyncio.to_thread(
                    store,
                    user_id,
                    conversation_id,
                    "assistant",
                    answer,
                )

                # 11. Send response to frontend
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
        logger.info("Chat connection closed.")

    except Exception:
        logger.exception("WebSocket connection failed")

    finally:
        db.close()
