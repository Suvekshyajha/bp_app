import base64
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import history
from conftest import login, make_user
from models import Conversation, Message

T0 = datetime(2026, 11, 1, 12, 0, 0)  # naive UTC, like SQLite returns


def add_chat(db, user, chat_id, created=T0, title="chat"):
    db.add(Conversation(id=chat_id, user_id=user.id, title=title, created_at=created))
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


# ---------- cursor helpers ----------

def test_cursor_round_trip():
    cursor = history.encode_cursor(T0, "abc")
    assert history.decode_cursor(cursor) == (T0, "abc")


def test_cursor_with_timezone_becomes_naive_utc():
    aware = datetime(2026, 11, 1, 17, 45, tzinfo=timezone(timedelta(hours=5, minutes=45)))
    moment, _ = history.decode_cursor(history.encode_cursor(aware, "x"))

    assert moment == datetime(2026, 11, 1, 12, 0)
    assert moment.tzinfo is None


@pytest.mark.parametrize("bad", [
    "not-base64!!",
    base64.urlsafe_b64encode(b"not json").decode(),
    base64.urlsafe_b64encode(json.dumps(["not a date", "id"]).encode()).decode(),
    base64.urlsafe_b64encode(json.dumps(["2026-11-01T12:00:00"]).encode()).decode(),
])
def test_bad_cursor_is_a_400(bad):
    with pytest.raises(HTTPException) as error:
        history.decode_cursor(bad)

    assert error.value.status_code == 400


def test_iso_utc_labels_naive_times_as_utc():
    assert history.iso_utc(T0).endswith("+00:00")


# ---------- GET /conversations ----------

def test_conversations_require_login(client):
    assert client.get("/api/conversations").status_code == 401


def test_conversations_most_recently_active_first(logged_in, db, user):
    add_chat(db, user, "old-chat-1", created=T0)
    add_chat(db, user, "new-chat-1", created=T0 + timedelta(hours=1))
    # The old chat gets a message later, so it is now the most active
    add_message(db, "old-chat-1", T0 + timedelta(hours=5))

    ids = [c["id"] for c in logged_in.get("/api/conversations").json()["items"]]

    assert ids == ["old-chat-1", "new-chat-1"]


def test_empty_chat_counts_as_used_when_it_was_created(logged_in, db, user):
    add_chat(db, user, "empty-chat", created=T0)

    item = logged_in.get("/api/conversations").json()["items"][0]

    assert item["updated_at"] == history.iso_utc(T0)


def test_conversations_only_show_my_chats(logged_in, db, user):
    other = make_user(db, "bob")
    add_chat(db, user, "mine-1234")
    add_chat(db, other, "theirs-1234")

    ids = [c["id"] for c in logged_in.get("/api/conversations").json()["items"]]

    assert ids == ["mine-1234"]


def test_conversations_pages_cover_everything_exactly_once(logged_in, db, user):
    for i in range(7):
        add_chat(db, user, f"chat-{i:04d}", created=T0 + timedelta(minutes=i))

    seen, cursor, pages = [], None, 0

    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        body = logged_in.get("/api/conversations", params=params).json()
        seen += [c["id"] for c in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert pages == 3                                   # 3 + 3 + 1
    assert seen == [f"chat-{i:04d}" for i in range(6, -1, -1)]  # newest first, no repeats


def test_last_page_has_no_next_cursor(logged_in, db, user):
    for i in range(3):
        add_chat(db, user, f"chat-{i:04d}", created=T0 + timedelta(minutes=i))

    body = logged_in.get("/api/conversations", params={"limit": 3}).json()

    assert len(body["items"]) == 3
    assert body["next_cursor"] is None


def test_chats_with_the_same_time_are_not_skipped_between_pages(logged_in, db, user):
    """The id in the cursor breaks ties, so equal timestamps lose nothing."""
    for i in range(5):
        add_chat(db, user, f"tie-{i:04d}", created=T0)

    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = logged_in.get("/api/conversations", params=params).json()
        seen += [c["id"] for c in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert sorted(seen) == [f"tie-{i:04d}" for i in range(5)]
    assert len(seen) == len(set(seen))


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_conversations_limit_is_validated(logged_in, limit):
    assert logged_in.get("/api/conversations", params={"limit": limit}).status_code == 422


def test_conversations_invalid_cursor_is_400(logged_in):
    assert logged_in.get("/api/conversations", params={"cursor": "garbage"}).status_code == 400


# ---------- GET /conversations/{id}/messages ----------

def seed_messages(db, user, count, chat_id="chat-0001"):
    add_chat(db, user, chat_id)
    for i in range(count):
        add_message(db, chat_id, T0 + timedelta(seconds=i), content=f"m{i}")


def test_messages_require_login(client):
    assert client.get("/api/conversations/x/messages").status_code == 401


def test_latest_page_comes_first_and_reads_oldest_to_newest(logged_in, db, user):
    seed_messages(db, user, 10)

    body = logged_in.get("/api/conversations/chat-0001/messages", params={"limit": 4}).json()

    assert [m["content"] for m in body["items"]] == ["m6", "m7", "m8", "m9"]
    assert body["next_cursor"] is not None


def test_next_cursor_walks_back_to_older_messages(logged_in, db, user):
    seed_messages(db, user, 10)
    url = "/api/conversations/chat-0001/messages"

    first = logged_in.get(url, params={"limit": 4}).json()
    second = logged_in.get(url, params={"limit": 4, "cursor": first["next_cursor"]}).json()
    third = logged_in.get(url, params={"limit": 4, "cursor": second["next_cursor"]}).json()

    assert [m["content"] for m in second["items"]] == ["m2", "m3", "m4", "m5"]
    assert [m["content"] for m in third["items"]] == ["m0", "m1"]
    assert third["next_cursor"] is None


def test_walking_all_pages_rebuilds_the_whole_chat_in_order(logged_in, db, user):
    seed_messages(db, user, 25)
    url = "/api/conversations/chat-0001/messages"

    pages, cursor = [], None
    while True:
        params = {"limit": 10, **({"cursor": cursor} if cursor else {})}
        body = logged_in.get(url, params=params).json()
        pages.insert(0, body["items"])  # older pages go in front, like the UI does
        cursor = body["next_cursor"]
        if cursor is None:
            break

    contents = [m["content"] for page in pages for m in page]
    assert contents == [f"m{i}" for i in range(25)]


def test_messages_with_the_same_time_are_not_lost_between_pages(logged_in, db, user):
    add_chat(db, user, "chat-0001")
    for i in range(6):
        add_message(db, "chat-0001", T0, msg_id=f"id-{i}")  # identical created_at
    url = "/api/conversations/chat-0001/messages"

    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = logged_in.get(url, params=params).json()
        seen += [m["id"] for m in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert sorted(seen) == [f"id-{i}" for i in range(6)]


def test_someone_elses_chat_looks_the_same_as_a_missing_chat(logged_in, db):
    other = make_user(db, "bob")
    add_chat(db, other, "bobs-chat-1")

    theirs = logged_in.get("/api/conversations/bobs-chat-1/messages")
    missing = logged_in.get("/api/conversations/does-not-exist/messages")

    assert theirs.status_code == missing.status_code == 404
    assert theirs.json() == missing.json()


def test_message_items_have_the_fields_the_frontend_reads(logged_in, db, user):
    seed_messages(db, user, 1)

    item = logged_in.get("/api/conversations/chat-0001/messages").json()["items"][0]

    assert set(item) == {"id", "role", "content", "created_at"}
    assert item["created_at"].endswith("+00:00")