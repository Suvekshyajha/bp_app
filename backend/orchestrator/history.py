"""The sidebar list and a chat's messages.

The data lives in the chat service now. These endpoints check who is asking
(the session cookie) and pass the question on over gRPC.
"""

import re

import grpc
from fastapi import APIRouter, Depends, HTTPException, Query, Response

import chat_client
from auth import get_current_user
from chat_client import ChatServiceError
from models import User
import logging

logger = logging.getLogger(__name__)


router = APIRouter(tags=["history"])

# Same rule the WebSocket applies to conversation ids
CONVERSATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{8,64}$")

CONVERSATIONS_PAGE_SIZE = 20
MESSAGES_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100


def to_http_error(error: ChatServiceError) -> HTTPException:
    logger.warning("Chat service error: %s %s", error.code.name, error.message)  # [log]
    if error.code == grpc.StatusCode.NOT_FOUND:
        return HTTPException(status_code=404, detail="Conversation not found.")

    if error.code == grpc.StatusCode.INVALID_ARGUMENT:
        return HTTPException(status_code=400, detail="Invalid cursor.")

    if error.code == grpc.StatusCode.DEADLINE_EXCEEDED:
        return HTTPException(status_code=504, detail="The chat service took too long.")

    return HTTPException(status_code=503, detail="The chat service is unavailable.")


@router.get("/conversations")
async def list_conversations(
    limit: int = Query(CONVERSATIONS_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    """The user's chats, most recently active first.

    Pass the `next_cursor` of one page as `cursor` to get the next page.
    `next_cursor` is null on the last page.
    """

    try:
        return await chat_client.list_conversations(user.id, limit, cursor)
    except ChatServiceError as error:
        raise to_http_error(error)


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    limit: int = Query(MESSAGES_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    """Messages of one chat, newest page first.

    Without a cursor you get the latest messages. `next_cursor` leads to the
    OLDER messages. Inside a page the messages are oldest first.
    """

    try:
        return await chat_client.list_messages(user.id, conversation_id, limit, cursor)
    except ChatServiceError as error:
        raise to_http_error(error)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str,
    user: User = Depends(get_current_user),
):
    """Delete a chat for good: its messages, its saved context and its
    entries in the search index. Other browsers stop seeing it too.

    404 if the chat does not exist or belongs to someone else.
    """

    logger.info("Delete conversation: user=%s conversation=%s", user.id, conversation_id)  # [log]
    if not CONVERSATION_ID_PATTERN.match(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")

    try:
        await chat_client.delete_conversation(user.id, conversation_id)
    except ChatServiceError as error:
        raise to_http_error(error)

    return Response(status_code=204)
