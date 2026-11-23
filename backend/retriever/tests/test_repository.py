"""Paging, ownership and saving, tested straight against SQLite."""

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import repository
from models import Conversation, Message
from repository import ConversationNotFound, InvalidCursor

T0 = datetime(2026, 11, 1, 12, 0, 0)  # naive UTC, like SQLite returns
ALICE = "alice-id"
BOB = "bob-id"


def add_chat(db, user_id, chat_id, created=T0, title="chat"):
    db.add(Conversation(id=chat_id, user_id=user_id, title=title, created_at=created))
    db.commit()


def add_message(db, chat_id, at, role="user", content="hi", msg_id=None):
    db.add(Message(
        id=msg_id or str(uuid.uuid4()),
        conversation_id=chat_id,
        role=role,
        content=content,
        created_at=at,
    ))
    db.commit()


def walk_conversations(user_id, limit):
    seen, cursor, pages = [], None, 0

    while True:
        items, cursor = repository.list_conversations(user_id, limit, cursor)
        seen += [item["id"] for item in items]
        pages += 1

        if cursor is None:
            return seen, pages


# ---------- cursor helpers ----------

def test_cursor_round_trip():
    cursor = repository.encode_cursor(T0, "abc")
    assert repository.decode_cursor(cursor) == (T0, "abc")


def test_cursor_with_timezone_becomes_naive_utc():
    aware = datetime(2026, 11, 1, 17, 45, tzinfo=timezone(timedelta(hours=5, minutes=45)))
    moment, _ = repository.decode_cursor(repository.encode_cursor(aware, "x"))

    assert moment == datetime(2026, 11, 1, 12, 0)
    assert moment.tzinfo is None


@pytest.mark.parametrize("bad", [
    "not-base64!!",
    base64.urlsafe_b64encode(b"not json").decode(),
    base64.urlsafe_b64encode(json.dumps(["not a date", "id"]).encode()).decode(),
    base64.urlsafe_b64encode(json.dumps(["2026-11-01T12:00:00"]).encode()).decode(),
])
def test_bad_cursor_is_rejected(bad):
    with pytest.raises(InvalidCursor):
        repository.decode_cursor(bad)


# ---------- writing ----------

def test_first_message_creates_the_chat_with_a_short_title(session_factory, db):
    assert repository.get_or_create_conversation(ALICE, "chat-0001", "  hello   there  ") is True

    chat = db.get(Conversation, "chat-0001")
    assert chat.user_id == ALICE
    assert chat.title == "hello there"


def test_title_is_cut_to_40_characters(session_factory, db):
    repository.get_or_create_conversation(ALICE, "chat-0001", "x" * 100)

    assert len(db.get(Conversation, "chat-0001").title) == 40


def test_someone_elses_chat_is_refused(session_factory, db):
    add_chat(db, BOB, "bobs-chat")

    assert repository.get_or_create_conversation(ALICE, "bobs-chat", "hi") is False


def test_own_chat_is_accepted_again(session_factory, db):
    add_chat(db, ALICE, "chat-0001")

    assert repository.get_or_create_conversation(ALICE, "chat-0001", "hi") is True


def test_history_is_oldest_first_and_limited(session_factory, db):
    add_chat(db, ALICE, "chat-0001")

    for i in range(repository.HISTORY_MAX_MESSAGES + 5):
        add_message(db, "chat-0001", T0 + timedelta(seconds=i), content=f"m{i}")

    history = repository.load_chat_history("chat-0001")

    assert len(history) == repository.HISTORY_MAX_MESSAGES
    assert history[0]["content"] == "m5"      # the 5 oldest were left out
    assert history[-1]["content"] == f"m{repository.HISTORY_MAX_MESSAGES + 4}"
    assert set(history[0]) == {"role", "content"}


def test_add_message_returns_its_id(session_factory, db):
    add_chat(db, ALICE, "chat-0001")

    message_id = repository.add_message("chat-0001", "assistant", "hello")

    saved = db.get(Message, message_id)
    assert (saved.role, saved.content) == ("assistant", "hello")


# ---------- list_conversations ----------

def test_conversations_most_recently_active_first(session_factory, db):
    add_chat(db, ALICE, "old-chat-1", created=T0)
    add_chat(db, ALICE, "new-chat-1", created=T0 + timedelta(hours=1))
    # The old chat gets a message later, so it is now the most active
    add_message(db, "old-chat-1", T0 + timedelta(hours=5))

    items, _ = repository.list_conversations(ALICE, 20, None)

    assert [i["id"] for i in items] == ["old-chat-1", "new-chat-1"]


def test_empty_chat_counts_as_used_when_it_was_created(session_factory, db):
    add_chat(db, ALICE, "empty-chat", created=T0)

    items, _ = repository.list_conversations(ALICE, 20, None)

    assert items[0]["updated_at"] == T0


def test_conversations_only_show_my_chats(session_factory, db):
    add_chat(db, ALICE, "mine-1234")
    add_chat(db, BOB, "theirs-1234")

    items, _ = repository.list_conversations(ALICE, 20, None)

    assert [i["id"] for i in items] == ["mine-1234"]


def test_conversation_pages_cover_everything_exactly_once(session_factory, db):
    for i in range(7):
        add_chat(db, ALICE, f"chat-{i:04d}", created=T0 + timedelta(minutes=i))

    seen, pages = walk_conversations(ALICE, 3)

    assert pages == 3                                            # 3 + 3 + 1
    assert seen == [f"chat-{i:04d}" for i in range(6, -1, -1)]   # newest first, no repeats


def test_last_page_has_no_next_cursor(session_factory, db):
    for i in range(3):
        add_chat(db, ALICE, f"chat-{i:04d}", created=T0 + timedelta(minutes=i))

    items, cursor = repository.list_conversations(ALICE, 3, None)

    assert len(items) == 3
    assert cursor is None


def test_chats_with_the_same_time_are_not_skipped_between_pages(session_factory, db):
    """The id in the cursor breaks ties, so equal timestamps lose nothing."""
    for i in range(5):
        add_chat(db, ALICE, f"tie-{i:04d}", created=T0)

    seen, _ = walk_conversations(ALICE, 2)

    assert sorted(seen) == [f"tie-{i:04d}" for i in range(5)]
    assert len(seen) == len(set(seen))


def test_conversations_invalid_cursor_is_rejected(session_factory):
    with pytest.raises(InvalidCursor):
        repository.list_conversations(ALICE, 20, "garbage")


# ---------- list_messages ----------

def seed_messages(db, count, chat_id="chat-0001", user_id=ALICE):
    add_chat(db, user_id, chat_id)
    for i in range(count):
        add_message(db, chat_id, T0 + timedelta(seconds=i), content=f"m{i}")


def test_latest_page_comes_first_and_reads_oldest_to_newest(session_factory, db):
    seed_messages(db, 10)

    items, cursor = repository.list_messages(ALICE, "chat-0001", 4, None)

    assert [m["content"] for m in items] == ["m6", "m7", "m8", "m9"]
    assert cursor is not None


def test_next_cursor_walks_back_to_older_messages(session_factory, db):
    seed_messages(db, 10)

    _, cursor = repository.list_messages(ALICE, "chat-0001", 4, None)
    second, cursor = repository.list_messages(ALICE, "chat-0001", 4, cursor)
    third, cursor = repository.list_messages(ALICE, "chat-0001", 4, cursor)

    assert [m["content"] for m in second] == ["m2", "m3", "m4", "m5"]
    assert [m["content"] for m in third] == ["m0", "m1"]
    assert cursor is None


def test_walking_all_pages_rebuilds_the_whole_chat_in_order(session_factory, db):
    seed_messages(db, 25)

    pages, cursor = [], None
    while True:
        items, cursor = repository.list_messages(ALICE, "chat-0001", 10, cursor)
        pages.insert(0, items)  # older pages go in front, like the UI does
        if cursor is None:
            break

    assert [m["content"] for page in pages for m in page] == [f"m{i}" for i in range(25)]


def test_messages_with_the_same_time_are_not_lost_between_pages(session_factory, db):
    add_chat(db, ALICE, "chat-0001")
    for i in range(6):
        add_message(db, "chat-0001", T0, msg_id=f"id-{i}")  # identical created_at

    seen, cursor = [], None
    while True:
        items, cursor = repository.list_messages(ALICE, "chat-0001", 2, cursor)
        seen += [m["id"] for m in items]
        if cursor is None:
            break

    assert sorted(seen) == [f"id-{i}" for i in range(6)]


def test_someone_elses_chat_looks_the_same_as_a_missing_chat(session_factory, db):
    add_chat(db, BOB, "bobs-chat-1")

    with pytest.raises(ConversationNotFound):
        repository.list_messages(ALICE, "bobs-chat-1", 10, None)

    with pytest.raises(ConversationNotFound):
        repository.list_messages(ALICE, "does-not-exist", 10, None)


def test_message_items_have_the_fields_the_service_sends(session_factory, db):
    seed_messages(db, 1)

    items, _ = repository.list_messages(ALICE, "chat-0001", 10, None)

    assert set(items[0]) == {"id", "role", "content", "created_at"}
