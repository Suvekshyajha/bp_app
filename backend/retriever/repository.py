"""Everything that reads or writes SQLite for the chat service.

These functions are plain synchronous code, each with its own short-lived
session. Callers that run inside the event loop call them with
`asyncio.to_thread(...)` so the loop is never blocked.
"""

import base64
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, tuple_
from sqlalchemy.exc import IntegrityError

from database import SessionLocal
from models import Conversation, Message


# Most messages of THIS chat read for each question.
# How many reach the model is decided in llm.py by the prompt budget.
HISTORY_MAX_MESSAGES = 20


class ConversationNotFound(Exception):
    """The chat does not exist, or belongs to someone else (same answer)."""


class InvalidCursor(ValueError):
    """The page token could not be read."""


# ---------- Page tokens ----------
#
# A page token is a bookmark: "continue after this row". It holds the time and
# id of the last row of the page, packed into an opaque string. The next
# request asks for rows after that bookmark, so rows added in the meantime
# never shift the pages (page numbers and offsets would).

def encode_cursor(moment: datetime, row_id: str) -> str:
    raw = json.dumps([moment.isoformat(), row_id])
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        moment_text, row_id = json.loads(
            base64.urlsafe_b64decode(cursor.encode())
        )
        moment = datetime.fromisoformat(moment_text)

        # SQLite keeps UTC times without a timezone label
        if moment.tzinfo is not None:
            moment = moment.astimezone(timezone.utc).replace(tzinfo=None)

        return moment, str(row_id)

    except (ValueError, TypeError):
        raise InvalidCursor("Invalid cursor.")


# ---------- Writing a turn ----------

def get_or_create_conversation(
    user_id: str,
    conversation_id: str,
    first_query: str,
) -> bool:
    """Make sure the chat exists. False if it belongs to another user."""

    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)

        if conversation:
            return conversation.user_id == user_id

        db.add(Conversation(
            id=conversation_id,
            user_id=user_id,
            title=" ".join(first_query.split())[:40],
        ))

        try:
            db.commit()
            return True

        except IntegrityError:
            # Two first messages arrived at once and the other one won
            db.rollback()
            conversation = db.get(Conversation, conversation_id)
            return conversation is not None and conversation.user_id == user_id


def load_chat_history(conversation_id: str) -> list[dict]:
    """The latest messages of one chat, oldest first, ready for the LLM.

    Call it BEFORE the new question is saved, so the question is not
    sent twice.
    """

    with SessionLocal() as db:
        rows = (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(HISTORY_MAX_MESSAGES)
            .all()
        )

    # rows are newest first; the chat is read oldest first
    return [
        {"role": row.role, "content": row.content}
        for row in reversed(rows)
    ]


def add_message(
    conversation_id: str,
    role: str,
    content: str,
    context: list[dict] | None = None,
    message_id: str | None = None,
) -> str:
    """Save a message. `context` is the list of related past messages an
    answer was written with, kept so the chat can show them again later.

    `message_id` lets the caller choose the id (the browser does this for the
    questions it sends, so both sides know the same id).

    Saving the same id twice into the same chat is safe: the first copy is
    kept and the id is returned again (this is what makes a repeated request
    harmless). An id that belongs to a different chat or role is an error.
    """

    message_id = message_id or str(uuid.uuid4())

    with SessionLocal() as db:
        db.add(Message(
            id=message_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            context=json.dumps(context) if context else None,
        ))

        try:
            db.commit()

        except IntegrityError:
            db.rollback()
            existing = db.get(Message, message_id)

            if (
                existing is None
                or existing.conversation_id != conversation_id
                or existing.role != role
            ):
                raise

    return message_id


def get_turn_state(
    user_id: str,
    conversation_id: str,
    message_id: str,
) -> dict | None:
    """What is already saved for a question the browser sent before.

    Returns None if this user message is not in the chat. Otherwise:
        {"query": the saved question,
         "answer": {"id", "content", "context"} or None}
    where "answer" is the assistant message that comes right after it.
    Raises ConversationNotFound if the chat is not this user's.
    """

    with SessionLocal() as db:
        owned = (
            db.query(Conversation.id)
            .filter(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
            .first()
        )

        if not owned:
            raise ConversationNotFound(conversation_id)

        question = (
            db.query(Message)
            .filter(
                Message.id == message_id,
                Message.conversation_id == conversation_id,
                Message.role == "user",
            )
            .first()
        )

        if question is None:
            return None

        reply = (
            db.query(Message)
            .filter(
                Message.conversation_id == conversation_id,
                tuple_(Message.created_at, Message.id)
                > tuple_(question.created_at, question.id),
            )
            .order_by(Message.created_at.asc(), Message.id.asc())
            .first()
        )

        answer = None

        if reply is not None and reply.role == "assistant":
            answer = {
                "id": reply.id,
                "content": reply.content,
                "context": parse_context(reply.context),
            }

        return {"query": question.content, "answer": answer}

def message_ids_from(
    user_id: str,
    conversation_id: str,
    message_id: str,
) -> list[str]:
    """The ids of this message and every message after it in the chat.

    Returns [] if the message is not in this chat (nothing to delete).
    Raises ConversationNotFound if the chat is not this user's.
    """

    with SessionLocal() as db:
        owned = (
            db.query(Conversation.id)
            .filter(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
            .first()
        )

        if not owned:
            raise ConversationNotFound(conversation_id)

        target = (
            db.query(Message)
            .filter(
                Message.id == message_id,
                Message.conversation_id == conversation_id,
            )
            .first()
        )

        if target is None:
            return []

        rows = (
            db.query(Message.id)
            .filter(
                Message.conversation_id == conversation_id,
                tuple_(Message.created_at, Message.id)
                >= tuple_(target.created_at, target.id),
            )
            .all()
        )

        return [row.id for row in rows]


def delete_messages(conversation_id: str, message_ids: list[str]) -> None:
    """Delete these messages of one chat. (The search index is cleaned
    separately, see qdrant_store.)"""

    if not message_ids:
        return

    with SessionLocal() as db:
        db.query(Message).filter(
            Message.conversation_id == conversation_id,
            Message.id.in_(message_ids),
        ).delete(synchronize_session=False)
        db.commit()

def conversation_exists(conversation_id: str) -> bool:
    with SessionLocal() as db:
        return db.get(Conversation, conversation_id) is not None


def delete_conversation(user_id: str, conversation_id: str) -> None:
    """Delete one of the user's chats and all its messages.

    Raises ConversationNotFound if there is no such chat for this user.
    (The search index is cleaned separately, see qdrant_store.)
    """

    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)

        if conversation is None or conversation.user_id != user_id:
            raise ConversationNotFound(conversation_id)

        # The relationship cascades: the messages go with the chat
        db.delete(conversation)
        db.commit()


def owns_conversation(user_id: str, conversation_id: str) -> bool:
    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)

        return conversation is not None and conversation.user_id == user_id


def parse_context(raw: str | None) -> list[dict]:
    """The saved related messages of an answer. Bad or missing data gives []."""

    if not raw:
        return []

    try:
        value = json.loads(raw)
    except ValueError:
        return []

    return value if isinstance(value, list) else []


# ---------- Reading history ----------

def list_conversations(
    user_id: str,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict], str | None]:
    """The user's chats, most recently active first, one page at a time."""

    with SessionLocal() as db:
        # When was each of this user's chats last used?
        last_message = (
            db.query(
                Message.conversation_id,
                func.max(Message.created_at).label("last_at"),
            )
            .filter(
                Message.conversation_id.in_(
                    db.query(Conversation.id).filter(
                        Conversation.user_id == user_id
                    )
                )
            )
            .group_by(Message.conversation_id)
            .subquery()
        )

        # A chat with no messages yet counts as used when it was created
        activity = func.coalesce(last_message.c.last_at, Conversation.created_at)

        query = (
            db.query(Conversation, activity.label("activity"))
            .outerjoin(
                last_message,
                last_message.c.conversation_id == Conversation.id,
            )
            .filter(Conversation.user_id == user_id)
        )

        if cursor:
            cursor_time, cursor_id = decode_cursor(cursor)

            query = query.filter(
                tuple_(activity, Conversation.id) < tuple_(cursor_time, cursor_id)
            )

        # One extra row tells us whether another page exists
        rows = (
            query
            .order_by(activity.desc(), Conversation.id.desc())
            .limit(limit + 1)
            .all()
        )

        has_more = len(rows) > limit
        rows = rows[:limit]

        items = [
            {
                "id": row.Conversation.id,
                "title": row.Conversation.title or "",
                "created_at": row.Conversation.created_at,
                "updated_at": row.activity,
            }
            for row in rows
        ]

    next_cursor = (
        encode_cursor(items[-1]["updated_at"], items[-1]["id"])
        if has_more
        else None
    )

    return items, next_cursor


def list_messages(
    user_id: str,
    conversation_id: str,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict], str | None]:
    """One page of a chat, newest page first.

    Without a cursor you get the latest messages. The next cursor leads to
    the OLDER ones. Inside a page the messages are in reading order.
    """

    with SessionLocal() as db:
        owned = (
            db.query(Conversation.id)
            .filter(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
            .first()
        )

        # Same answer for "does not exist" and "belongs to someone else"
        if not owned:
            raise ConversationNotFound(conversation_id)

        query = db.query(Message).filter(Message.conversation_id == conversation_id)

        if cursor:
            cursor_time, cursor_id = decode_cursor(cursor)

            query = query.filter(
                tuple_(Message.created_at, Message.id)
                < tuple_(cursor_time, cursor_id)
            )

        rows = (
            query
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit + 1)
            .all()
        )

        has_more = len(rows) > limit
        rows = rows[:limit]

        items = [
            {
                "id": row.id,
                "role": row.role,
                "content": row.content,
                "created_at": row.created_at,
                "context": parse_context(row.context),
            }
            for row in rows
        ]

    next_cursor = (
        encode_cursor(items[-1]["created_at"], items[-1]["id"])
        if has_more
        else None
    )

    items.reverse()  # oldest first, the order the chat is read in

    return items, next_cursor
