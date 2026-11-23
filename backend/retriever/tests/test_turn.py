"""run_turn: order of events, what gets saved, and what happens on stop/failure."""

import asyncio

import pytest

import repository
import turn
from conftest import drain_background
from models import Conversation, Message
from repository import ConversationNotFound
from turn import Context, Done, Token, run_turn

ALICE = "alice-id"
CHAT = "chat-0001"


async def collect(user_id=ALICE, conversation_id=CHAT, query="hello"):
    return [event async for event in run_turn(user_id, conversation_id, query)]


def saved_messages(db):
    db.expire_all()
    rows = db.query(Message).order_by(Message.created_at, Message.id).all()
    return [(m.role, m.content) for m in rows]


# ---------- the normal turn ----------

def test_events_come_in_order_and_both_messages_are_saved(session_factory, db, model, indexed, retrieved):
    async def scenario():
        events = await collect()
        await drain_background()
        return events

    events = asyncio.run(scenario())

    assert isinstance(events[0], Context)
    assert [e.text for e in events if isinstance(e, Token)] == ["Hel", "lo"]
    assert isinstance(events[-1], Done)
    assert events[-1].answer == "Hello"

    assert saved_messages(db) == [("user", "hello"), ("assistant", "Hello")]


def test_done_carries_the_ids_of_the_saved_messages(session_factory, db, model, indexed, retrieved):
    events = asyncio.run(collect())
    done = events[-1]

    db.expire_all()
    assert db.get(Message, done.user_message_id).content == "hello"
    assert db.get(Message, done.assistant_message_id).content == "Hello"


def test_both_messages_are_indexed_in_qdrant_in_the_background(session_factory, model, indexed, retrieved):
    async def scenario():
        await collect()
        await drain_background()

    asyncio.run(scenario())

    assert sorted(indexed) == [
        (ALICE, CHAT, "assistant", "Hello"),
        (ALICE, CHAT, "user", "hello"),
    ]


def test_the_model_gets_history_from_before_the_new_question(session_factory, model, indexed, retrieved):
    repository.get_or_create_conversation(ALICE, CHAT, "first")
    repository.add_message(CHAT, "user", "first")
    repository.add_message(CHAT, "assistant", "first answer")

    asyncio.run(collect(query="second"))

    call = model.calls[0]
    assert call["query"] == "second"
    assert call["history"] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "first answer"},
    ]  # the new question is not in its own history


def test_the_question_cannot_retrieve_itself(session_factory, model, indexed, retrieved, monkeypatch):
    """The question is indexed only AFTER the search has run."""
    order = []

    def fake_retrieve(**kwargs):
        order.append("search")
        return []

    def fake_store(**kwargs):
        order.append("index")

    monkeypatch.setattr(turn, "retrieve_messages", fake_retrieve)
    monkeypatch.setattr(turn, "store_message", fake_store)

    async def scenario():
        await collect()
        await drain_background()

    asyncio.run(scenario())

    assert order[0] == "search"


def test_search_is_limited_to_this_user_and_chat(session_factory, model, indexed, retrieved):
    asyncio.run(collect())

    call = retrieved.calls[0]
    assert call["user_id"] == ALICE
    assert call["conversation_id"] == CHAT
    assert call["query"] == "hello"


# ---------- context ----------

def hit(role, content, id="m"):
    return {"id": id, "score": 0.9, "payload": {"role": role, "content": content}}


def test_retrieved_messages_already_in_the_history_are_not_sent_twice(session_factory, model, indexed, retrieved):
    repository.get_or_create_conversation(ALICE, CHAT, "x")
    repository.add_message(CHAT, "user", "recent")

    retrieved.results = [hit("user", "recent", "a"), hit("user", "old topic", "b")]

    events = asyncio.run(collect())

    assert [r["id"] for r in events[0].results] == ["b"]
    assert [r["id"] for r in model.calls[0]["context"]] == ["b"]


def test_context_is_capped(session_factory, model, indexed, retrieved):
    retrieved.results = [hit("user", f"old {i}", str(i)) for i in range(20)]

    events = asyncio.run(collect())

    assert len(events[0].results) == turn.RETRIEVAL_LIMIT


def test_if_the_search_is_down_the_chat_still_answers(session_factory, db, model, indexed, retrieved):
    retrieved.error = RuntimeError("qdrant is down")

    events = asyncio.run(collect())

    assert events[0].results == []
    assert events[-1].answer == "Hello"
    assert saved_messages(db) == [("user", "hello"), ("assistant", "Hello")]


def test_if_indexing_fails_the_answer_is_still_saved(session_factory, db, model, retrieved, monkeypatch):
    def broken_store(**kwargs):
        raise RuntimeError("qdrant is down")

    monkeypatch.setattr(turn, "store_message", broken_store)

    async def scenario():
        events = await collect()
        await drain_background()
        return events

    events = asyncio.run(scenario())

    assert events[-1].answer == "Hello"
    assert saved_messages(db) == [("user", "hello"), ("assistant", "Hello")]


# ---------- ownership ----------

def test_someone_elses_chat_is_refused_and_nothing_is_saved(session_factory, db, model, indexed, retrieved):
    repository.get_or_create_conversation("bob-id", CHAT, "bobs question")

    with pytest.raises(ConversationNotFound):
        asyncio.run(collect(user_id=ALICE, query="intruder"))

    assert saved_messages(db) == []
    assert model.calls == []


# ---------- failure and stop ----------

def test_if_the_model_fails_no_half_answer_is_saved(session_factory, db, model, indexed, retrieved):
    model.tokens = ["a", "b", "c"]
    model.fail_after = 1

    with pytest.raises(RuntimeError):
        asyncio.run(collect())

    assert saved_messages(db) == [("user", "hello")]  # the question stays
    assert model.closed is True


def test_stop_saves_what_was_written_and_closes_the_model_stream(session_factory, db, model, indexed, retrieved):
    model.tokens = ["a", "b", "c", "d"]
    model.block_after = 2  # writes "a", "b", then waits (until stopped)

    async def scenario():
        received = []

        async def consume():
            async for event in run_turn(ALICE, CHAT, "hello"):
                if isinstance(event, Token):
                    received.append(event.text)

        task = asyncio.create_task(consume())

        while len(received) < 2:
            await asyncio.sleep(0.01)

        task.cancel()  # the stop button

        with pytest.raises(asyncio.CancelledError):
            await task

        await drain_background()
        return received

    received = asyncio.run(scenario())

    assert received == ["a", "b"]
    assert saved_messages(db) == [("user", "hello"), ("assistant", "ab")]
    assert model.closed is True  # the Groq stream was closed, not left running
    assert (ALICE, CHAT, "assistant", "ab") in indexed


def test_stop_before_any_token_saves_no_empty_answer(session_factory, db, model, indexed, retrieved):
    model.block_after = 0  # waits before the first token

    async def scenario():
        async def consume():
            async for _ in run_turn(ALICE, CHAT, "hello"):
                pass

        task = asyncio.create_task(consume())

        while not model.calls:
            await asyncio.sleep(0.01)

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        await drain_background()

    asyncio.run(scenario())

    assert saved_messages(db) == [("user", "hello")]
