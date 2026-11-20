from datetime import datetime, timedelta, timezone

import pytest
from starlette.websockets import WebSocketDisconnect

import auth
import websocket
from conftest import login
from models import Message, UserSession
from rate_limit import SlidingWindowRateLimiter

CHAT = "conv-12345678"


def receive_until_done(ws):
    """Collect messages until the 'done' message of an answer."""
    received = []
    while True:
        message = ws.receive_json()
        received.append(message)
        if message.get("type") == "done" or "error" in message:
            return received


@pytest.fixture(autouse=True)
def generous_limiter(monkeypatch):
    """Default: a limit so high it never gets in the way."""
    monkeypatch.setattr(
        websocket, "chat_limiter", SlidingWindowRateLimiter(1000, 60)
    )


def naive_utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------- connection checks ----------

def test_no_session_cookie_is_closed_with_4401(client):
    with client.websocket_connect("/ws/chat") as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()

    assert closed.value.code == websocket.WS_AUTH_FAILED


def test_origin_that_is_not_allowed_is_closed_with_4403(logged_in):
    with logged_in.websocket_connect(
        "/ws/chat", headers={"origin": "http://evil.example"}
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()

    assert closed.value.code == websocket.WS_ORIGIN_BLOCKED


# ---------- heartbeat ----------

def test_ping_is_answered_with_pong(logged_in):
    with logged_in.websocket_connect("/ws/chat") as ws:
        ws.send_json({"type": "ping"})

        assert ws.receive_json() == {"type": "pong"}


def test_many_pings_do_not_use_up_the_chat_rate_limit(logged_in, monkeypatch):
    monkeypatch.setattr(websocket, "chat_limiter", SlidingWindowRateLimiter(1, 60))

    with logged_in.websocket_connect("/ws/chat") as ws:
        for _ in range(5):
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}

        # The single allowed chat message still goes through
        ws.send_json({"query": "hello", "conversation_id": CHAT})
        assert receive_until_done(ws)[-1]["type"] == "done"


def test_ping_does_not_extend_the_session_but_a_message_does(
    logged_in, db, user
):
    soon = naive_utc_now() + timedelta(minutes=1)
    db.query(UserSession).filter(UserSession.user_id == user.id).update(
        {"expires_at": soon}
    )
    db.commit()

    def expires_at():
        db.expire_all()
        return db.query(UserSession).filter(UserSession.user_id == user.id).one().expires_at

    with logged_in.websocket_connect("/ws/chat") as ws:
        ws.send_json({"type": "ping"})
        ws.receive_json()
        assert expires_at() == soon  # a ping is not "activity"

        ws.send_json({"query": "hello", "conversation_id": CHAT})
        receive_until_done(ws)
        assert expires_at() > soon + timedelta(minutes=5)  # a real message is


def test_ping_on_an_expired_session_closes_with_4401(logged_in, db, user):
    with logged_in.websocket_connect("/ws/chat") as ws:
        db.query(UserSession).filter(UserSession.user_id == user.id).update(
            {"expires_at": naive_utc_now() - timedelta(minutes=1)}
        )
        db.commit()

        ws.send_json({"type": "ping"})

        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()

    assert closed.value.code == websocket.WS_AUTH_FAILED


def test_logging_out_ends_an_open_socket_on_the_next_ping(logged_in, db, user):
    with logged_in.websocket_connect("/ws/chat") as ws:
        db.query(UserSession).filter(UserSession.user_id == user.id).delete()
        db.commit()

        ws.send_json({"type": "ping"})

        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()

    assert closed.value.code == websocket.WS_AUTH_FAILED


# ---------- rate limit ----------

def test_message_over_the_limit_gets_an_error_with_retry_after(logged_in, monkeypatch):
    monkeypatch.setattr(websocket, "chat_limiter", SlidingWindowRateLimiter(1, 60))

    with logged_in.websocket_connect("/ws/chat") as ws:
        ws.send_json({"query": "first", "conversation_id": CHAT})
        assert receive_until_done(ws)[-1]["type"] == "done"

        ws.send_json({"query": "second", "conversation_id": CHAT})
        error = ws.receive_json()

    assert "too quickly" in error["error"]
    assert error["conversation_id"] == CHAT      # frontend matches replies by this
    assert isinstance(error["retry_after"], int)  # frontend builds RateLimitError from this
    assert 1 <= error["retry_after"] <= 60


def test_rate_limited_message_is_not_saved(logged_in, db, monkeypatch):
    monkeypatch.setattr(websocket, "chat_limiter", SlidingWindowRateLimiter(1, 60))

    with logged_in.websocket_connect("/ws/chat") as ws:
        ws.send_json({"query": "first", "conversation_id": CHAT})
        receive_until_done(ws)
        ws.send_json({"query": "blocked", "conversation_id": CHAT})
        ws.receive_json()

    contents = [m.content for m in db.query(Message).filter(Message.role == "user")]
    assert contents == ["first"]


def test_the_limit_is_per_user(client, db, user, monkeypatch):
    from conftest import make_user

    monkeypatch.setattr(websocket, "chat_limiter", SlidingWindowRateLimiter(1, 60))

    login(client, db, user)
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"query": "alice", "conversation_id": CHAT})
        receive_until_done(ws)

    bob = make_user(db, "bob")
    login(client, db, bob)  # the cookie now belongs to bob
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"query": "bob", "conversation_id": "conv-87654321"})
        assert receive_until_done(ws)[-1]["type"] == "done"


# ---------- chat flow and validation ----------

def test_answer_streams_tokens_then_done_and_both_messages_are_saved(logged_in, db):
    with logged_in.websocket_connect("/ws/chat") as ws:
        ws.send_json({"query": "  hello  ", "conversation_id": CHAT})
        received = receive_until_done(ws)

    tokens = [m["content"] for m in received if m.get("type") == "token"]
    done = received[-1]

    assert tokens == ["Hel", "lo"]
    assert done["answer"] == "Hello"
    assert done["query"] == "hello"            # trimmed
    assert done["conversation_id"] == CHAT
    assert done["cancelled"] is False

    saved = db.query(Message).order_by(Message.created_at).all()
    assert [(m.role, m.content) for m in saved] == [
        ("user", "hello"),
        ("assistant", "Hello"),
    ]


def test_invalid_json_gets_an_error_and_the_socket_stays_open(logged_in):
    with logged_in.websocket_connect("/ws/chat") as ws:
        ws.send_text("this is not json")
        assert ws.receive_json() == {"error": "Invalid JSON."}

        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


@pytest.mark.parametrize("payload", [
    {"query": ""},
    {"query": "   "},
    {"query": 123},
    {},
])
def test_empty_or_non_string_query_is_rejected(logged_in, payload):
    with logged_in.websocket_connect("/ws/chat") as ws:
        ws.send_json({**payload, "conversation_id": CHAT})
        reply = ws.receive_json()

    assert reply["error"] == "A valid query is required."
    assert reply["conversation_id"] == CHAT


def test_invalid_conversation_id_is_rejected(logged_in):
    with logged_in.websocket_connect("/ws/chat") as ws:
        ws.send_json({"query": "hi", "conversation_id": "../../etc"})

        assert ws.receive_json() == {"error": "Invalid conversation id."}


def test_cannot_write_into_another_users_conversation(client, db, user):
    from conftest import make_user

    login(client, db, user)
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"query": "mine", "conversation_id": CHAT})
        receive_until_done(ws)

    bob = make_user(db, "bob")
    login(client, db, bob)
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"query": "intruder", "conversation_id": CHAT})
        reply = ws.receive_json()

    assert reply["error"] == "Conversation not found."
    assert "intruder" not in [m.content for m in db.query(Message)]


# ---------- in-conversation history ----------

def test_second_question_gets_the_first_exchange_as_history(logged_in, monkeypatch):
    seen = []

    async def spy_stream(query, results, history=None):
        seen.append({"query": query, "history": history})
        yield "answer to " + query

    monkeypatch.setattr(websocket, "stream_response", spy_stream)

    with logged_in.websocket_connect("/ws/chat") as ws:
        ws.send_json({"query": "first", "conversation_id": CHAT})
        receive_until_done(ws)

        ws.send_json({"query": "second", "conversation_id": CHAT})
        receive_until_done(ws)

    assert seen[0]["history"] == []
    assert seen[1]["history"] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer to first"},
    ]  # the new question itself is not in its own history


def test_history_never_comes_from_another_chat(logged_in, monkeypatch):
    seen = []

    async def spy_stream(query, results, history=None):
        seen.append(history)
        yield "ok"

    monkeypatch.setattr(websocket, "stream_response", spy_stream)

    with logged_in.websocket_connect("/ws/chat") as ws:
        ws.send_json({"query": "in chat one", "conversation_id": CHAT})
        receive_until_done(ws)

        ws.send_json({"query": "in chat two", "conversation_id": "conv-87654321"})
        receive_until_done(ws)

    assert seen[1] == []


def test_load_chat_history_keeps_the_newest_messages_in_reading_order(
    db, user, monkeypatch
):
    from models import Conversation

    monkeypatch.setattr(websocket, "HISTORY_MAX_MESSAGES", 3)

    db.add(Conversation(id=CHAT, user_id=user.id, title="t"))
    base = naive_utc_now()

    for number in range(5):
        db.add(Message(
            id=f"m{number}",
            conversation_id=CHAT,
            role="user" if number % 2 == 0 else "assistant",
            content=f"message {number}",
            created_at=base + timedelta(seconds=number),
        ))
    db.commit()

    history = websocket.load_chat_history(db, CHAT)

    assert [m["content"] for m in history] == [
        "message 2", "message 3", "message 4"
    ]


def test_load_chat_history_stops_at_the_size_budget(db, user, monkeypatch):
    from models import Conversation

    monkeypatch.setattr(websocket, "HISTORY_MAX_CHARS", 100)

    db.add(Conversation(id=CHAT, user_id=user.id, title="t"))
    base = naive_utc_now()

    for number in range(3):
        db.add(Message(
            id=f"m{number}",
            conversation_id=CHAT,
            role="user",
            content=f"{number}" * 60,
            created_at=base + timedelta(seconds=number),
        ))
    db.commit()

    history = websocket.load_chat_history(db, CHAT)

    # Only the newest fits in 100 characters
    assert [m["content"] for m in history] == ["2" * 60]


# ---------- retrieval is limited to one user and one chat ----------

def test_retrieval_is_asked_for_this_user_and_this_chat_only(
    logged_in, user, monkeypatch
):
    calls = []

    async def spy_retrieve(query, user_id, conversation_id, limit):
        calls.append((query, user_id, conversation_id))
        return []

    monkeypatch.setattr(websocket, "retrieve", spy_retrieve)

    with logged_in.websocket_connect("/ws/chat") as ws:
        ws.send_json({"query": "in chat one", "conversation_id": CHAT})
        receive_until_done(ws)

        ws.send_json({"query": "in chat two", "conversation_id": "conv-87654321"})
        receive_until_done(ws)

    assert calls == [
        ("in chat one", user.id, CHAT),
        ("in chat two", user.id, "conv-87654321"),
    ]