"""The chat service end to end over real gRPC, with the model, the embeddings
and Qdrant replaced by fakes. Covers: saved context, deleting a chat, and the
shared service token."""

import asyncio
import importlib
import sys
import types

import grpc
import pytest
import pytest_asyncio

import chat_pb2
import chat_pb2_grpc

# ---------- Fakes, installed only while the service modules are imported ----------

indexed: dict[str, dict] = {}   # message_id -> {"user_id", "conversation_id"}
deleted_calls: list[tuple] = []
retrieved: list[dict] = []      # what the fake search returns next
fail_index_delete = {"on": False}


def _fake_modules():
    store = types.ModuleType("qdrant_store")
    store.create_collection = lambda: None
    store.COLLECTION_NAME = "chat_messages"
    store.client = object()

    def store_message(user_id, conversation_id, role, content, message_id=None):
        indexed[message_id] = {"user_id": user_id, "conversation_id": conversation_id}
        return message_id

    def delete_points(user_id, conversation_id):
        if fail_index_delete["on"]:
            raise RuntimeError("qdrant down")
        deleted_calls.append((user_id, conversation_id))
        for key in [
            k for k, v in indexed.items()
            if v["user_id"] == user_id and v["conversation_id"] == conversation_id
        ]:
            del indexed[key]

    store.store_message = store_message
    store.delete_conversation_points = delete_points

    retrieval = types.ModuleType("retrieval")
    retrieval.retrieve_messages = lambda **kwargs: list(retrieved)

    llm = types.ModuleType("llm")

    async def stream_response(query, results, history):
        for piece in ["Hel", "lo"]:
            yield piece

    llm.stream_response = stream_response

    return {"qdrant_store": store, "retrieval": retrieval, "llm": llm}


def _import_service():
    names = ["qdrant_store", "retrieval", "llm", "turn", "main"]
    saved = {n: sys.modules.get(n) for n in names}

    for n in names:
        sys.modules.pop(n, None)

    sys.modules.update(_fake_modules())

    try:
        main = importlib.import_module("main")
        main.turn_module = sys.modules["turn"]
    finally:
        for n in names:
            sys.modules.pop(n, None)
            if saved[n] is not None:
                sys.modules[n] = saved[n]

    return main


service = _import_service()

import repository  # noqa: E402
from database import Base, engine, migrate_schema  # noqa: E402

TOKEN = "s3cret-token"


@pytest_asyncio.fixture
async def stubs():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    migrate_schema()
    indexed.clear()
    deleted_calls.clear()
    retrieved.clear()
    fail_index_delete["on"] = False

    server = grpc.aio.server(interceptors=[service.ServiceTokenInterceptor(TOKEN)])
    chat_pb2_grpc.add_ChatServiceServicer_to_server(service.ChatServicer(), server)
    chat_pb2_grpc.add_HistoryServiceServicer_to_server(service.HistoryServicer(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    yield (
        chat_pb2_grpc.ChatServiceStub(channel),
        chat_pb2_grpc.HistoryServiceStub(channel),
        [("authorization", f"Bearer {TOKEN}")],
    )

    await channel.close()
    await server.stop(None)


async def ask(chat, auth, user, conversation, query):
    events = []
    async for event in chat.Chat(
        chat_pb2.ChatRequest(user_id=user, conversation_id=conversation, query=query),
        metadata=auth,
    ):
        events.append(event)
    return events


async def settle():
    """Let the background indexing tasks finish."""
    for _ in range(5):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


def related(message_id="old-1", content="we talked about cats"):
    return {
        "id": message_id,
        "score": 0.83,
        "payload": {"role": "user", "content": content, "timestamp": "2026-11-01T10:00:00+00:00"},
    }


# ---------- Saved context ----------

@pytest.mark.asyncio
async def test_related_messages_are_stored_with_the_answer(stubs):
    chat, history, auth = stubs
    retrieved.append(related())

    await ask(chat, auth, "u1", "conv-0001-aaaa", "tell me about cats")

    page = await history.ListMessages(
        chat_pb2.ListMessagesRequest(user_id="u1", conversation_id="conv-0001-aaaa"),
        metadata=auth,
    )
    user_msg, answer = page.messages

    assert answer.content == "Hello"
    assert [m.content for m in answer.context] == ["we talked about cats"]
    assert answer.context[0].similarity == pytest.approx(0.83, abs=1e-4)
    assert answer.context[0].role == chat_pb2.ROLE_USER
    assert answer.context[0].created_at.seconds > 0
    assert list(user_msg.context) == []


@pytest.mark.asyncio
async def test_context_is_encrypted_in_the_database(stubs):
    chat, _, auth = stubs
    retrieved.append(related(content="very private words"))

    await ask(chat, auth, "u1", "conv-0001-aaaa", "hi")

    with engine.connect() as connection:
        raw = connection.exec_driver_sql("SELECT content, context FROM messages").fetchall()
    assert raw
    for content, context in raw:
        assert "private" not in (content or "") + (context or "")


@pytest.mark.asyncio
async def test_answers_without_context_and_old_rows_come_back_empty(stubs):
    chat, history, auth = stubs

    await ask(chat, auth, "u1", "conv-0001-aaaa", "hi")
    # A row written before the column existed has NULL there
    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE messages SET context = NULL")

    page = await history.ListMessages(
        chat_pb2.ListMessagesRequest(user_id="u1", conversation_id="conv-0001-aaaa"),
        metadata=auth,
    )

    assert all(list(m.context) == [] for m in page.messages)


def test_migrate_adds_the_column_to_an_old_database():
    with engine.begin() as c:
        c.exec_driver_sql("DROP TABLE IF EXISTS messages")
        c.exec_driver_sql(
            "CREATE TABLE messages (id VARCHAR PRIMARY KEY, conversation_id VARCHAR, "
            "role VARCHAR, content TEXT, created_at DATETIME)"
        )

    migrate_schema()
    migrate_schema()  # safe to run twice

    with engine.connect() as c:
        columns = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(messages)")}

    assert "context" in columns
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


# ---------- Qdrant and SQLite share one id ----------

@pytest.mark.asyncio
async def test_search_index_uses_the_same_ids_as_the_database(stubs):
    chat, _, auth = stubs

    events = await ask(chat, auth, "u1", "conv-0001-aaaa", "hi")
    await settle()

    done = events[-1].done

    assert set(indexed) == {done.user_message_id, done.assistant_message_id}


# ---------- Delete ----------

@pytest.mark.asyncio
async def test_delete_removes_messages_and_index_entries(stubs):
    chat, history, auth = stubs

    await ask(chat, auth, "u1", "conv-0001-aaaa", "hi")
    await ask(chat, auth, "u1", "conv-0002-bbbb", "other chat")
    await settle()
    assert len(indexed) == 4

    await history.DeleteConversation(
        chat_pb2.DeleteConversationRequest(user_id="u1", conversation_id="conv-0001-aaaa"),
        metadata=auth,
    )

    listing = await history.ListConversations(
        chat_pb2.ListConversationsRequest(user_id="u1"), metadata=auth
    )
    assert [c.id for c in listing.conversations] == ["conv-0002-bbbb"]
    assert deleted_calls == [("u1", "conv-0001-aaaa")]
    assert {v["conversation_id"] for v in indexed.values()} == {"conv-0002-bbbb"}

    with engine.connect() as c:
        left = c.exec_driver_sql("SELECT DISTINCT conversation_id FROM messages").fetchall()
    assert left == [("conv-0002-bbbb",)]

    with pytest.raises(grpc.aio.AioRpcError) as error:
        await history.ListMessages(
            chat_pb2.ListMessagesRequest(user_id="u1", conversation_id="conv-0001-aaaa"),
            metadata=auth,
        )
    assert error.value.code() == grpc.StatusCode.NOT_FOUND


@pytest.mark.asyncio
async def test_cannot_delete_someone_elses_chat(stubs):
    chat, history, auth = stubs
    await ask(chat, auth, "u1", "conv-0001-aaaa", "hi")

    with pytest.raises(grpc.aio.AioRpcError) as error:
        await history.DeleteConversation(
            chat_pb2.DeleteConversationRequest(user_id="intruder", conversation_id="conv-0001-aaaa"),
            metadata=auth,
        )

    assert error.value.code() == grpc.StatusCode.NOT_FOUND
    assert deleted_calls == []
    assert repository.conversation_exists("conv-0001-aaaa")


@pytest.mark.asyncio
async def test_deleting_twice_reports_not_found(stubs):
    chat, history, auth = stubs
    await ask(chat, auth, "u1", "conv-0001-aaaa", "hi")
    request = chat_pb2.DeleteConversationRequest(user_id="u1", conversation_id="conv-0001-aaaa")

    await history.DeleteConversation(request, metadata=auth)

    with pytest.raises(grpc.aio.AioRpcError) as error:
        await history.DeleteConversation(request, metadata=auth)

    assert error.value.code() == grpc.StatusCode.NOT_FOUND


@pytest.mark.asyncio
async def test_if_the_index_is_down_nothing_is_deleted(stubs):
    chat, history, auth = stubs
    await ask(chat, auth, "u1", "conv-0001-aaaa", "hi")
    fail_index_delete["on"] = True

    with pytest.raises(grpc.aio.AioRpcError) as error:
        await history.DeleteConversation(
            chat_pb2.DeleteConversationRequest(user_id="u1", conversation_id="conv-0001-aaaa"),
            metadata=auth,
        )

    assert error.value.code() == grpc.StatusCode.UNAVAILABLE
    assert repository.conversation_exists("conv-0001-aaaa")


@pytest.mark.asyncio
async def test_a_deleted_chat_is_not_re_indexed(stubs):
    chat, _, auth = stubs
    await ask(chat, auth, "u1", "conv-0001-aaaa", "hi")
    await settle()
    repository.delete_conversation("u1", "conv-0001-aaaa")
    indexed.clear()

    await service.turn_module._index("u1", "conv-0001-aaaa", "user", "late", "late-id")

    assert indexed == {}


# ---------- Service token ----------

@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [[], [("authorization", "Bearer wrong")]])
async def test_calls_without_the_right_token_are_refused(stubs, metadata):
    chat, history, _ = stubs

    with pytest.raises(grpc.aio.AioRpcError) as error:
        await history.ListConversations(
            chat_pb2.ListConversationsRequest(user_id="u1"), metadata=metadata
        )
    assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED

    with pytest.raises(grpc.aio.AioRpcError) as error:
        await ask(chat, metadata, "u1", "conv-0001-aaaa", "hi")
    assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED

    assert not repository.conversation_exists("conv-0001-aaaa")
