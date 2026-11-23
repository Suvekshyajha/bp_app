from datetime import datetime, timedelta, timezone

import grpc
import pytest
from starlette.websockets import WebSocketDisconnect

import auth
import websocket
from conftest import login
from models import UserSession
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


def test_rate_limited_message_never_reaches_the_chat_service(
    logged_in, fake_service, monkeypatch
):
    monkeypatch.setattr(websocket, "chat_limiter", SlidingWindowRateLimiter(1, 60))

    with logged_in.websocket_connect("/ws/chat") as ws:
        ws.send_json({"query": "first", "conversation_id": CHAT})
        receive_until_done(ws)
        ws.send_json({"query": "blocked", "conversation_id": CHAT})
        ws.receive_json()

    assert [call[2] for call in fake_service.calls] == ["first"]


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

def test_answer_streams_tokens_then_done(logged_in, user, fake_service):
    fake_service.results = [{"id": "m1", "score": 0.9, "payload": {"role": "user", "content": "earlier"}}]

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
    assert done["results"] == fake_service.results   # the sources reach the browser

    # The chat service got the verified user id and the trimmed question
    assert fake_service.calls == [(user.id, CHAT, "hello")]


@pytest.mark.parametrize("code, message", [
    (grpc.StatusCode.NOT_FOUND, "Conversation not found."),
    (grpc.StatusCode.RESOURCE_EXHAUSTED, "busy"),
    (grpc.StatusCode.DEADLINE_EXCEEDED, "too long"),
    (grpc.StatusCode.UNAVAILABLE, "unavailable"),
    (grpc.StatusCode.INTERNAL, "Something went wrong."),
])
def test_chat_service_errors_become_friendly_messages(
    logged_in, fake_service, code, message
):
    from conftest import ChatServiceError

    fake_service.error = ChatServiceError(code, "details from the service")

    with logged_in.websocket_connect("/ws/chat") as ws:
        ws.send_json({"query": "hi", "conversation_id": CHAT})
        reply = ws.receive_json()

    assert message in reply["error"]
    assert reply["conversation_id"] == CHAT
    assert "details from the service" not in reply["error"]  # never leak internals


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


def test_chat_service_saying_not_found_is_reported_to_the_browser(logged_in, fake_service):
    """The service answers NOT_FOUND for a chat that belongs to someone else.
    (The ownership rule itself is tested in the chat service.)"""
    from conftest import ChatServiceError

    fake_service.error = ChatServiceError(grpc.StatusCode.NOT_FOUND, "nope")

    with logged_in.websocket_connect("/ws/chat") as ws:
        ws.send_json({"query": "intruder", "conversation_id": CHAT})
        reply = ws.receive_json()

    assert reply["error"] == "Conversation not found."
