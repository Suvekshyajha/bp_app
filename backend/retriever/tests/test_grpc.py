"""The real gRPC server and generated stubs, talking over a real local port.
Only the model, the search and Qdrant are fakes."""

import asyncio
from contextlib import asynccontextmanager

import grpc
import groq
import httpx
import pytest

import chat_pb2
import chat_pb2_grpc
import main
import repository
from conftest import drain_background


@asynccontextmanager
async def running_server():
    server = grpc.aio.server()
    chat_pb2_grpc.add_ChatServiceServicer_to_server(main.ChatServicer(), server)
    chat_pb2_grpc.add_HistoryServiceServicer_to_server(main.HistoryServicer(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        yield (
            chat_pb2_grpc.ChatServiceStub(channel),
            chat_pb2_grpc.HistoryServiceStub(channel),
        )

    await server.stop(grace=1)


def request(user="alice-id", chat="chat-0001", query="hello"):
    return chat_pb2.ChatRequest(user_id=user, conversation_id=chat, query=query)


def run(coroutine_function):
    return asyncio.run(coroutine_function())


# ---------- chat ----------

def test_events_arrive_context_then_tokens_then_done(session_factory, model, indexed, retrieved):
    retrieved.results = [{
        "id": "m1",
        "score": 0.8,
        "payload": {"role": "user", "content": "earlier", "timestamp": "2026-11-01T12:00:00+00:00"},
    }]

    async def scenario():
        async with running_server() as (chat, _):
            events = [event async for event in chat.Chat(request())]
            await drain_background()
            return events

    events = run(scenario)
    kinds = [event.WhichOneof("event") for event in events]

    assert kinds == ["context", "token", "token", "done"]

    context = events[0].context.messages[0]
    assert (context.id, context.role, context.content) == ("m1", chat_pb2.ROLE_USER, "earlier")
    assert context.similarity == pytest.approx(0.8)
    assert context.created_at.seconds > 0

    assert events[-1].done.answer == "Hello"
    assert events[-1].done.user_message_id
    assert events[-1].done.assistant_message_id


@pytest.mark.parametrize("bad", [
    request(user=""),
    request(chat=""),
    request(query=""),
    request(query="   "),
])
def test_missing_fields_are_invalid_argument(session_factory, model, indexed, retrieved, bad):
    async def scenario():
        async with running_server() as (chat, _):
            with pytest.raises(grpc.aio.AioRpcError) as error:
                async for _ in chat.Chat(bad):
                    pass
            return error.value.code()

    assert run(scenario) == grpc.StatusCode.INVALID_ARGUMENT


def test_someone_elses_chat_is_not_found(session_factory, model, indexed, retrieved):
    repository.get_or_create_conversation("bob-id", "chat-0001", "bobs")

    async def scenario():
        async with running_server() as (chat, _):
            with pytest.raises(grpc.aio.AioRpcError) as error:
                async for _ in chat.Chat(request(user="alice-id")):
                    pass
            return error.value.code()

    assert run(scenario) == grpc.StatusCode.NOT_FOUND


def groq_error(cls):
    http_request = httpx.Request("POST", "http://groq.test")

    if cls is groq.RateLimitError:
        return cls("limit", response=httpx.Response(429, request=http_request), body=None)

    if cls is groq.APITimeoutError:
        return cls(request=http_request)

    return cls(request=http_request)


@pytest.mark.parametrize("error_class, status", [
    (groq.RateLimitError, grpc.StatusCode.RESOURCE_EXHAUSTED),
    (groq.APITimeoutError, grpc.StatusCode.DEADLINE_EXCEEDED),
    (groq.APIConnectionError, grpc.StatusCode.UNAVAILABLE),
])
def test_model_problems_become_the_right_status(
    session_factory, model, indexed, retrieved, monkeypatch, error_class, status
):
    import turn

    async def failing_stream(query, context, history=None):
        raise groq_error(error_class)
        yield  # makes this an async generator

    monkeypatch.setattr(turn, "stream_response", failing_stream)

    async def scenario():
        async with running_server() as (chat, _):
            with pytest.raises(grpc.aio.AioRpcError) as error:
                async for _ in chat.Chat(request()):
                    pass
            return error.value.code()

    assert run(scenario) == status


def test_unexpected_errors_are_internal_and_hide_details(session_factory, model, indexed, retrieved):
    model.tokens = ["a", "b"]
    model.fail_after = 0

    async def scenario():
        async with running_server() as (chat, _):
            with pytest.raises(grpc.aio.AioRpcError) as error:
                async for _ in chat.Chat(request()):
                    pass
            return error.value

    error = run(scenario)

    assert error.code() == grpc.StatusCode.INTERNAL
    assert "model failed" not in error.details()


def test_cancelling_the_call_stops_the_model_and_saves_the_partial_answer(
    session_factory, db, model, indexed, retrieved
):
    """This is the stop button, all the way through a real gRPC call."""
    model.tokens = ["a", "b", "c", "d"]
    model.block_after = 2

    async def scenario():
        async with running_server() as (chat, _):
            call = chat.Chat(request())
            received = []

            async for event in call:
                if event.WhichOneof("event") == "token":
                    received.append(event.token.text)

                if len(received) == 2:
                    call.cancel()
                    break

            # give the server a moment to notice and finish saving
            for _ in range(100):
                await asyncio.sleep(0.02)
                db.expire_all()
                if db.query(repository.Message).filter_by(role="assistant").count():
                    break

            await drain_background()
            return received

    received = run(scenario)

    assert received == ["a", "b"]

    db.expire_all()
    saved = [(m.role, m.content) for m in db.query(repository.Message).order_by(repository.Message.created_at)]
    assert saved == [("user", "hello"), ("assistant", "ab")]
    assert model.closed is True


# ---------- history ----------

def test_history_round_trip(session_factory, model, indexed, retrieved):
    async def scenario():
        async with running_server() as (chat, history):
            async for _ in chat.Chat(request(query="first question")):
                pass
            await drain_background()

            conversations = await history.ListConversations(
                chat_pb2.ListConversationsRequest(user_id="alice-id", page_size=10)
            )
            messages = await history.ListMessages(
                chat_pb2.ListMessagesRequest(
                    user_id="alice-id", conversation_id="chat-0001", page_size=10
                )
            )
            return conversations, messages

    conversations, messages = run(scenario)

    assert [c.title for c in conversations.conversations] == ["first question"]
    assert conversations.next_page_token == ""
    assert conversations.conversations[0].updated_at.seconds > 0

    assert [(m.role, m.content) for m in messages.messages] == [
        (chat_pb2.ROLE_USER, "first question"),
        (chat_pb2.ROLE_ASSISTANT, "Hello"),
    ]


def test_history_errors(session_factory):
    repository.get_or_create_conversation("bob-id", "bobs-chat", "x")

    async def code_of(call):
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await call
        return error.value.code()

    async def scenario():
        async with running_server() as (_, history):
            return [
                await code_of(history.ListMessages(chat_pb2.ListMessagesRequest(
                    user_id="alice-id", conversation_id="bobs-chat"))),
                await code_of(history.ListMessages(chat_pb2.ListMessagesRequest(
                    user_id="alice-id", conversation_id="bobs-chat", page_token="garbage"))),
                await code_of(history.ListConversations(chat_pb2.ListConversationsRequest(
                    user_id="alice-id", page_token="garbage"))),
                await code_of(history.ListConversations(chat_pb2.ListConversationsRequest())),
            ]

    codes = run(scenario)

    assert codes == [
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.NOT_FOUND,          # ownership is checked before the cursor
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.INVALID_ARGUMENT,
    ]

# ---------- idempotency over the wire ----------

def test_the_same_user_message_id_twice_gives_the_same_answer_once(session_factory, model, indexed, retrieved, monkeypatch):
    import turn

    monkeypatch.setattr(turn, "delete_message_points", lambda ids: None)
    key = "22222222-2222-4222-8222-222222222222"

    async def scenario():
        async with running_server() as (chat, _):
            call = lambda: chat_pb2.ChatRequest(
                user_id="alice-id", conversation_id="chat-0001",
                query="hello", user_message_id=key,
            )
            first = [e async for e in chat.Chat(call())]
            await drain_background()
            second = [e async for e in chat.Chat(call())]
            await drain_background()
            return first, second

    first, second = run(scenario)

    assert len(model.calls) == 1
    assert first[-1].done.user_message_id == second[-1].done.user_message_id == key
    assert first[-1].done.assistant_message_id == second[-1].done.assistant_message_id
    assert second[-1].done.answer == "Hello"


def test_a_user_message_id_that_is_not_a_uuid_is_invalid_argument(session_factory, model, indexed, retrieved):
    async def scenario():
        async with running_server() as (chat, _):
            bad = chat_pb2.ChatRequest(
                user_id="alice-id", conversation_id="chat-0001",
                query="hello", user_message_id="not-a-uuid",
            )
            with pytest.raises(grpc.aio.AioRpcError) as error:
                [e async for e in chat.Chat(bad)]
            return error.value.code()

    assert run(scenario) == grpc.StatusCode.INVALID_ARGUMENT

# ---------- prompt-injection hygiene on the way in ----------

def test_hidden_characters_are_removed_before_the_question_is_saved(session_factory, db, model, indexed, retrieved):
    from models import Message

    async def scenario():
        async with running_server() as (chat, _):
            events = [e async for e in chat.Chat(request(query="hel\u200blo\U000e0041"))]
            await drain_background()
            return events

    run(scenario)

    db.expire_all()
    saved = db.query(Message).filter(Message.role == "user").one()

    assert saved.content == "hello"
    assert model.calls[0]["query"] == "hello"


@pytest.mark.parametrize("bad", ["\u200b\u200b", "x" * 4001])
def test_empty_after_cleaning_or_too_long_is_invalid_argument(session_factory, model, indexed, retrieved, bad):
    async def scenario():
        async with running_server() as (chat, _):
            with pytest.raises(grpc.aio.AioRpcError) as error:
                [e async for e in chat.Chat(request(query=bad))]
            return error.value.code()

    assert run(scenario) == grpc.StatusCode.INVALID_ARGUMENT
    assert model.calls == []


# ---------- documents (step 5) ----------

DOC = "11111111-1111-4111-8111-111111111111"


def doc_request(document_ids, query="hello"):
    return chat_pb2.ChatRequest(
        user_id="alice-id",
        conversation_id="chat-0001",
        query=query,
        document_ids=document_ids,
    )


def failure_of(call):
    async def scenario():
        async with running_server() as (chat, _):
            with pytest.raises(grpc.aio.AioRpcError) as error:
                async for _ in chat.Chat(call):
                    pass
            return error.value

    return run(scenario)


def test_a_missing_document_is_not_found(session_factory, db, model, indexed, retrieved, doc_search):
    doc_search.states[DOC] = "missing"

    error = failure_of(doc_request([DOC]))

    assert error.code() == grpc.StatusCode.NOT_FOUND
    assert error.details() == "Document not found."
    assert model.calls == []


def test_a_document_still_processing_is_failed_precondition(session_factory, db, model, indexed, retrieved, doc_search):
    doc_search.states[DOC] = "processing"

    error = failure_of(doc_request([DOC]))

    assert error.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert error.details() == "Document is still being processed."
    assert model.calls == []


@pytest.mark.parametrize("bad", [["not-a-uuid"], [DOC, "123"]])
def test_document_ids_must_be_uuids(session_factory, model, indexed, retrieved, doc_search, bad):
    error = failure_of(doc_request(bad))

    assert error.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert doc_search.state_calls == []


def test_at_most_five_documents_can_be_searched(session_factory, model, indexed, retrieved, doc_search):
    import uuid

    ids = [str(uuid.uuid4()) for _ in range(6)]

    error = failure_of(doc_request(ids))

    assert error.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert doc_search.state_calls == []


def test_the_same_document_twice_is_searched_once(session_factory, model, indexed, retrieved, doc_search):
    async def scenario():
        async with running_server() as (chat, _):
            return [e async for e in chat.Chat(doc_request([DOC, DOC.upper()]))]

    run(scenario)

    assert doc_search.state_calls == [("alice-id", [DOC])]


def test_excerpts_arrive_with_file_name_and_place(session_factory, model, indexed, retrieved, doc_search):
    doc_search.results = [{
        "id": "c1",
        "score": 0.8,
        "payload": {
            "document_id": DOC,
            "filename": "report.pdf",
            "chunk_index": 2,
            "section_title": "Results",
            "page": 3,
            "content": "Revenue was five.",
            "role": None,
            "timestamp": "2026-11-01T10:00:00+00:00",
        },
    }]

    async def scenario():
        async with running_server() as (chat, _):
            events = [e async for e in chat.Chat(doc_request([DOC]))]
            await drain_background()
            return events

    events = run(scenario)
    excerpt = events[0].context.messages[0]

    assert (excerpt.document_id, excerpt.filename) == (DOC, "report.pdf")
    assert (excerpt.chunk_index, excerpt.section_title, excerpt.page) == (2, "Results", 3)
    assert excerpt.content == "Revenue was five."
    assert events[-1].done.answer == "Hello"
