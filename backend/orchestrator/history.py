"""The sidebar list and a chat's messages.

The data lives in the chat service now. These endpoints check who is asking
(the session cookie) and pass the question on over gRPC.
"""

import grpc
from fastapi import APIRouter, Depends, HTTPException, Query

import chat_client
from auth import get_current_user
from chat_client import ChatServiceError
from models import User


router = APIRouter(tags=["history"])

CONVERSATIONS_PAGE_SIZE = 20
MESSAGES_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100


def to_http_error(error: ChatServiceError) -> HTTPException:
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
