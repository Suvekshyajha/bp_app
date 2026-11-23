"""Paging, ownership and deleting, against a real (throw-away) SQLite file."""

from datetime import datetime, timedelta

import pytest

import repository
from database import Base, SessionLocal, engine, migrate_schema
from models import Message


@pytest.fixture(autouse=True)
def fresh_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    migrate_schema()


def add(conversation_id, n, user_id="u1", role="user", context=None):
    """Save a message and give it a distinct, increasing time."""

    repository.get_or_create_conversation(user_id, conversation_id, f"first {conversation_id}")
    message_id = repository.add_message(conversation_id, role, f"m{n}", context)

    with SessionLocal() as db:
        row = db.get(Message, message_id)
        row.created_at = datetime(2026, 1, 1) + timedelta(minutes=n)
        db.commit()

    return message_id


def test_messages_page_newest_first_and_read_oldest_first():
    for n in range(1, 8):
        add("chat-0001", n)

    items, cursor = repository.list_messages("u1", "chat-0001", 3, None)
    assert [m["content"] for m in items] == ["m5", "m6", "m7"]
    assert cursor

    older, cursor = repository.list_messages("u1", "chat-0001", 3, cursor)
    assert [m["content"] for m in older] == ["m2", "m3", "m4"]

    oldest, cursor = repository.list_messages("u1", "chat-0001", 3, cursor)
    assert [m["content"] for m in oldest] == ["m1"]
    assert cursor is None


def test_conversations_are_listed_by_latest_activity():
    add("chat-0001", 1)
    add("chat-0002", 2)
    add("chat-0001", 3)  # chat 1 is active again

    items, _ = repository.list_conversations("u1", 10, None)

    assert [c["id"] for c in items] == ["chat-0001", "chat-0002"]


def test_conversations_page_without_repeats():
    for n in range(5):
        add(f"chat-000{n}", n)

    first, cursor = repository.list_conversations("u1", 2, None)
    second, cursor = repository.list_conversations("u1", 2, cursor)
    third, cursor = repository.list_conversations("u1", 2, cursor)

    ids = [c["id"] for c in first + second + third]
    assert ids == ["chat-0004", "chat-0003", "chat-0002", "chat-0001", "chat-0000"]
    assert cursor is None


def test_a_bad_cursor_is_refused():
    with pytest.raises(repository.InvalidCursor):
        repository.list_conversations("u1", 5, "not-a-cursor")


def test_someone_elses_chat_looks_like_it_does_not_exist():
    add("chat-0001", 1, user_id="alice")

    with pytest.raises(repository.ConversationNotFound):
        repository.list_messages("bob", "chat-0001", 5, None)

    assert repository.list_conversations("bob", 5, None)[0] == []
    assert repository.get_or_create_conversation("bob", "chat-0001", "hi") is False


def test_context_round_trips():
    saved = [{"id": "x", "score": 0.7, "role": "user", "content": "cats", "timestamp": None}]
    add("chat-0001", 1, role="assistant", context=saved)

    items, _ = repository.list_messages("u1", "chat-0001", 5, None)

    assert items[0]["context"] == saved


@pytest.mark.parametrize("raw", [None, "", "not json", '{"a": 1}', '"text"'])
def test_unreadable_context_gives_an_empty_list(raw):
    assert repository.parse_context(raw) == []


def test_delete_removes_the_chat_and_its_messages_only():
    add("chat-0001", 1)
    add("chat-0001", 2)
    add("chat-0002", 3)

    repository.delete_conversation("u1", "chat-0001")

    assert not repository.conversation_exists("chat-0001")
    assert repository.conversation_exists("chat-0002")

    with SessionLocal() as db:
        assert {m.conversation_id for m in db.query(Message).all()} == {"chat-0002"}


def test_delete_refuses_other_users_and_missing_chats():
    add("chat-0001", 1, user_id="alice")

    with pytest.raises(repository.ConversationNotFound):
        repository.delete_conversation("bob", "chat-0001")

    with pytest.raises(repository.ConversationNotFound):
        repository.delete_conversation("alice", "nope-0000")

    assert repository.conversation_exists("chat-0001")
