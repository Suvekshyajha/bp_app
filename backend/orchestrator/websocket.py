import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from retriever_client import retrieve, store
from llm import generate_response


logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/chat")
async def chat(websocket: WebSocket):
    await websocket.accept()

    try:
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
            user_id = data.get("user_id")
            conversation_id = data.get("conversation_id")

            if (
                not isinstance(query, str)
                or not isinstance(user_id, str)
                or not isinstance(conversation_id, str)
                or not query.strip()
                or not user_id.strip()
                or not conversation_id.strip()
            ):
                await websocket.send_json({
                    "error": "Query, user_id, and conversation_id are required."
                })
                continue

            try:
                # 1. Retrieve relevant past messages
                results = await asyncio.to_thread(
                    retrieve,
                    query,
                    user_id,
                    5,
                )

                # 2. Save the current user message
                await asyncio.to_thread(
                    store,
                    user_id,
                    conversation_id,
                    "user",
                    query,
                )

                # 3. Generate an AI response using Groq
                answer = await asyncio.to_thread(
                    generate_response,
                    query,
                    results,
                )

                # 4. Save the assistant response
                await asyncio.to_thread(
                    store,
                    user_id,
                    conversation_id,
                    "assistant",
                    answer,
                )

                # 5. Send the response to the frontend
                await websocket.send_json({
                    "query": query,
                    "answer": answer,
                    "conversation_id": conversation_id,
                    "results": results,
                })

            except Exception:
                logger.exception("Chat request failed")
                await websocket.send_json({
                    "error": "Something went wrong. Please try again."
                })

    except WebSocketDisconnect:
        logger.info("Chat connection closed.")