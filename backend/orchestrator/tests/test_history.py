"""The history endpoints only check the login and pass the question on to
the chat service. The paging logic itself is tested in the chat service
(retriever/tests/test_repository.py)."""

import grpc
import pytest

from conftest import ChatServiceError


# ---------- GET /conversations ----------

def test_conversations_require_login(client):
    assert client.get("/api/conversations").status_code == 401


def test_conversations_are_asked_for_with_the_logged_in_user(logged_in, user, fake_service):
    fake_service.conversations = {
        "items": [{"id": "c1", "title": "t", "created_at": "x", "updated_at": "y"}],
        "next_cursor": "next",
    }

    body = logged_in.get("/api/conversations", params={"limit": 5, "cursor": "abc"}).json()

    assert body == fake_service.conversations
    assert fake_service.calls == [("list_conversations", user.id, 5, "abc")]


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_conversations_limit_is_validated(logged_in, limit):
    assert logged_in.get("/api/conversations", params={"limit": limit}).status_code == 422


@pytest.mark.parametrize("code, status", [
    (grpc.StatusCode.INVALID_ARGUMENT, 400),
    (grpc.StatusCode.DEADLINE_EXCEEDED, 504),
    (grpc.StatusCode.UNAVAILABLE, 503),
    (grpc.StatusCode.INTERNAL, 503),
])
def test_conversation_errors_map_to_http_status(logged_in, fake_service, code, status):
    fake_service.history_error = ChatServiceError(code, "x")

    assert logged_in.get("/api/conversations").status_code == status


# ---------- GET /conversations/{id}/messages ----------

def test_messages_require_login(client):
    assert client.get("/api/conversations/x/messages").status_code == 401


def test_messages_are_asked_for_with_the_logged_in_user(logged_in, user, fake_service):
    fake_service.messages = {
        "items": [{"id": "m1", "role": "user", "content": "hi", "created_at": "x"}],
        "next_cursor": None,
    }

    body = logged_in.get(
        "/api/conversations/chat-0001/messages", params={"limit": 4}
    ).json()

    assert body == fake_service.messages
    assert fake_service.calls == [("list_messages", user.id, "chat-0001", 4, None)]


def test_missing_or_foreign_chat_is_a_404(logged_in, fake_service):
    fake_service.history_error = ChatServiceError(grpc.StatusCode.NOT_FOUND, "x")

    response = logged_in.get("/api/conversations/bobs-chat-1/messages")

    assert response.status_code == 404
    assert response.json() == {"detail": "Conversation not found."}


def test_saved_context_is_passed_through_with_the_messages(logged_in, fake_service):
    related = {"id": "m0", "score": 0.8, "payload": {"role": "user", "content": "cats"}}
    fake_service.messages = {
        "items": [
            {"id": "m1", "role": "assistant", "content": "hi", "created_at": "x", "results": [related]},
        ],
        "next_cursor": None,
    }

    body = logged_in.get("/api/conversations/chat-0001/messages").json()

    assert body["items"][0]["results"] == [related]


# ---------- DELETE /conversations/{id} ----------

def test_delete_requires_login(client):
    assert client.delete("/api/conversations/chat-0001").status_code == 401


def test_delete_is_asked_for_with_the_logged_in_user(logged_in, user, fake_service):
    response = logged_in.delete("/api/conversations/chat-0001")

    assert response.status_code == 204
    assert response.content == b""
    assert fake_service.calls == [("delete_conversation", user.id, "chat-0001")]


@pytest.mark.parametrize("bad_id", ["short", "has_underscore_in_it", "x" * 65, "..%2f..%2fetc"])
def test_delete_refuses_malformed_ids_without_calling_the_service(logged_in, fake_service, bad_id):
    assert logged_in.delete(f"/api/conversations/{bad_id}").status_code == 404
    assert fake_service.calls == []


def test_deleting_a_missing_or_foreign_chat_is_a_404(logged_in, fake_service):
    fake_service.history_error = ChatServiceError(grpc.StatusCode.NOT_FOUND, "x")

    response = logged_in.delete("/api/conversations/bobs-chat-1")

    assert response.status_code == 404
    assert response.json() == {"detail": "Conversation not found."}


@pytest.mark.parametrize("code, status", [
    (grpc.StatusCode.UNAVAILABLE, 503),
    (grpc.StatusCode.DEADLINE_EXCEEDED, 504),
])
def test_delete_errors_map_to_http_status(logged_in, fake_service, code, status):
    fake_service.history_error = ChatServiceError(code, "x")

    assert logged_in.delete("/api/conversations/chat-0001").status_code == status
