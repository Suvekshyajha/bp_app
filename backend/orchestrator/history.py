import base64
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models import Conversation, Message, User


router = APIRouter(tags=["history"])

CONVERSATIONS_PAGE_SIZE = 20
MESSAGES_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100


# ---------- Cursors ----------
#
# A cursor is a bookmark: "continue after this row". It holds the time and
# id of the last row of the page, packed into an opaque string. The next
# request asks for rows that come after that bookmark, so rows added in the
# meantime never shift the pages (page numbers and offsets would).

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
        raise HTTPException(status_code=400, detail="Invalid cursor.")


def iso_utc(value: datetime) -> str:
    """SQLite returns UTC times without a timezone label. Add it, or the
    browser would read them as local time."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return value.isoformat()


# ---------- Endpoints ----------

@router.get("/conversations")
def list_conversations(
    limit: int = Query(CONVERSATIONS_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The user's chats, most recently active first.

    Pass the `next_cursor` of one page as `cursor` to get the next page.
    `next_cursor` is null on the last page.
    """

    # When was each of this user's chats last used?
    last_message = (
        db.query(
            Message.conversation_id,
            func.max(Message.created_at).label("last_at"),
        )
        .filter(
            Message.conversation_id.in_(
                db.query(Conversation.id).filter(
                    Conversation.user_id == user.id
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
        .filter(Conversation.user_id == user.id)
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

    next_cursor = (
        encode_cursor(rows[-1].activity, rows[-1].Conversation.id)
        if has_more
        else None
    )

    return {
        "items": [
            {
                "id": row.Conversation.id,
                "title": row.Conversation.title,
                "created_at": iso_utc(row.Conversation.created_at),
                "updated_at": iso_utc(row.activity),
            }
            for row in rows
        ],
        "next_cursor": next_cursor,
    }


@router.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: str,
    limit: int = Query(MESSAGES_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Messages of one chat, newest page first.

    Without a cursor you get the latest messages. `next_cursor` leads to the
    OLDER messages (for a "load earlier messages" button or scrolling up).
    Inside a page the messages are in reading order, oldest first.
    """

    owned = (
        db.query(Conversation.id)
        .filter(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
        .first()
    )

    # Same answer for "does not exist" and "belongs to someone else"
    if not owned:
        raise HTTPException(status_code=404, detail="Conversation not found.")

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

    next_cursor = (
        encode_cursor(rows[-1].created_at, rows[-1].id)
        if has_more
        else None
    )

    rows.reverse()  # oldest first, the order the chat is read in

    return {
        "items": [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "created_at": iso_utc(message.created_at),
            }
            for message in rows
        ],
        "next_cursor": next_cursor,
    }