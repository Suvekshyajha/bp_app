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

# ---------- idempotency: the same question sent twice ----------

QUESTION_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def no_index_cleanup(monkeypatch):
    """Deleting old messages also cleans Qdrant; do nothing there in tests."""

    monkeypatch.setattr(turn, "delete_message_points", lambda ids: None)


async def collect_with_id(query="hello", regenerate=False, message_id=QUESTION_ID):
    return [
        event
        async for event in run_turn(
            ALICE, CHAT, query, user_message_id=message_id, regenerate=regenerate
        )
    ]


def test_sending_the_same_question_twice_replays_the_answer(session_factory, db, model, indexed, retrieved, no_index_cleanup):
    async def scenario():
        first = await collect_with_id()
        await drain_background()
        second = await collect_with_id()
        await drain_background()
        return first, second

    first, second = asyncio.run(scenario())

    assert len(model.calls) == 1  # the model was asked once
    assert saved_messages(db) == [("user", "hello"), ("assistant", "Hello")]

    assert isinstance(second[0], Context)
    assert "".join(e.text for e in second if isinstance(e, Token)) == "Hello"
    assert second[-1].user_message_id == first[-1].user_message_id == QUESTION_ID
    assert second[-1].assistant_message_id == first[-1].assistant_message_id


def test_two_requests_at_the_same_time_run_the_model_once(session_factory, db, model, indexed, retrieved, no_index_cleanup):
    async def scenario():
        results = await asyncio.gather(collect_with_id(), collect_with_id())
        await drain_background()
        return results

    first, second = asyncio.run(scenario())

    assert len(model.calls) == 1
    assert saved_messages(db) == [("user", "hello"), ("assistant", "Hello")]
    assert first[-1].answer == second[-1].answer == "Hello"


def test_retry_after_a_failed_answer_asks_the_model_again(session_factory, db, model, indexed, retrieved, no_index_cleanup):
    model.fail_after = 1

    async def scenario():
        with pytest.raises(RuntimeError):
            await collect_with_id()

        model.fail_after = None  # the model works again

        events = await collect_with_id()
        await drain_background()
        return events

    events = asyncio.run(scenario())

    assert len(model.calls) == 2
    assert events[-1].answer == "Hello"
    assert saved_messages(db) == [("user", "hello"), ("assistant", "Hello")]


def test_regenerate_asks_the_model_again_and_keeps_one_answer(session_factory, db, model, indexed, retrieved, no_index_cleanup):
    async def scenario():
        await collect_with_id()
        await drain_background()

        model.tokens = ["New", " answer"]
        events = await collect_with_id(regenerate=True)
        await drain_background()
        return events

    events = asyncio.run(scenario())

    assert len(model.calls) == 2
    assert events[-1].answer == "New answer"
    assert saved_messages(db) == [("user", "hello"), ("assistant", "New answer")]


def test_editing_the_text_replaces_the_old_question_and_answer(session_factory, db, model, indexed, retrieved, no_index_cleanup):
    async def scenario():
        await collect_with_id(query="hello")
        await drain_background()

        model.tokens = ["Edited"]
        events = await collect_with_id(query="hello there")
        await drain_background()
        return events

    events = asyncio.run(scenario())

    assert len(model.calls) == 2
    assert events[-1].answer == "Edited"
    assert saved_messages(db) == [("user", "hello there"), ("assistant", "Edited")]


def test_a_message_id_cannot_reach_into_someone_elses_chat(session_factory, db, model, indexed, retrieved, no_index_cleanup):
    asyncio.run(collect_with_id())

    async def mallory():
        return [e async for e in run_turn("mallory-id", CHAT, "hello", user_message_id=QUESTION_ID)]

    with pytest.raises(ConversationNotFound):
        asyncio.run(mallory())

    assert len(model.calls) == 1


def test_the_claim_is_released_even_when_the_turn_fails(session_factory, model, indexed, retrieved, no_index_cleanup):
    model.fail_after = 0

    with pytest.raises(RuntimeError):
        asyncio.run(collect_with_id())

    assert turn._running == {}


# ---------- documents (step 5) ----------

import uuid

DOC = "11111111-1111-4111-8111-111111111111"
OTHER_DOC = "22222222-2222-4222-8222-222222222222"


def doc_hit(id="c1", content="Revenue was five."):
    return {
        "id": id,
        "score": 0.8,
        "payload": {
            "document_id": DOC,
            "filename": "report.pdf",
            "chunk_index": 2,
            "section_title": "Results",
            "page": 3,
            "content": content,
            "role": None,
            "timestamp": "2026-11-01T10:00:00+00:00",
        },
    }


async def collect_docs(document_ids, user_id=ALICE, query="hello", **kwargs):
    return [
        event
        async for event in run_turn(user_id, CHAT, query, document_ids=document_ids, **kwargs)
    ]


def test_a_document_that_is_still_processing_saves_nothing(session_factory, db, model, indexed, retrieved, doc_search):
    doc_search.states[DOC] = "processing"

    with pytest.raises(turn.DocumentNotReady):
        asyncio.run(collect_docs([DOC]))

    assert saved_messages(db) == []
    assert model.calls == []
    assert indexed == []


def test_a_missing_or_foreign_document_saves_nothing(session_factory, db, model, indexed, retrieved, doc_search):
    doc_search.states[DOC] = "missing"  # what doc_retrieval says for another user's document

    with pytest.raises(turn.DocumentNotFound):
        asyncio.run(collect_docs([DOC]))

    assert saved_messages(db) == []
    assert model.calls == []
    assert doc_search.calls == []


def test_a_missing_document_wins_over_a_processing_one(session_factory, model, indexed, retrieved, doc_search):
    doc_search.states = {DOC: "processing", OTHER_DOC: "missing"}

    with pytest.raises(turn.DocumentNotFound):
        asyncio.run(collect_docs([DOC, OTHER_DOC]))


def test_the_failed_message_can_be_sent_again_when_the_document_is_ready(session_factory, db, model, indexed, retrieved, doc_search):
    question = str(uuid.uuid4())
    doc_search.states[DOC] = "processing"

    with pytest.raises(turn.DocumentNotReady):
        asyncio.run(collect_docs([DOC], user_message_id=question))

    doc_search.states[DOC] = "ready"
    events = asyncio.run(collect_docs([DOC], user_message_id=question))

    assert events[-1].answer == "Hello"
    assert saved_messages(db) == [("user", "hello"), ("assistant", "Hello")]


def test_the_checks_and_the_search_use_the_callers_user_id(session_factory, model, indexed, retrieved, doc_search):
    doc_search.results = [doc_hit()]

    asyncio.run(collect_docs([DOC]))

    assert doc_search.state_calls == [(ALICE, [DOC])]
    assert doc_search.calls[0]["user_id"] == ALICE
    assert doc_search.calls[0]["document_ids"] == [DOC]
    assert doc_search.calls[0]["limit"] == turn.DOCUMENT_CONTEXT_LIMIT


def test_excerpts_reach_the_model_apart_from_the_chat_context(session_factory, model, indexed, retrieved, doc_search):
    retrieved.results = [hit("user", "old topic", "old")]
    doc_search.results = [doc_hit()]

    events = asyncio.run(collect_docs([DOC]))

    call = model.calls[0]
    assert [r["id"] for r in call["context"]] == ["old"]
    assert [r["id"] for r in call["documents"]] == ["c1"]
    assert [r["id"] for r in events[0].results] == ["old", "c1"]


def test_a_user_without_any_documents_is_not_searched_or_embedded(session_factory, model, indexed, retrieved, doc_search):
    """No documents at all: the turn runs exactly as it did before documents
    existed. Only the cheap look at the user's library happens."""

    asyncio.run(collect())

    assert doc_search.library_calls == [ALICE]
    assert doc_search.state_calls == []
    assert doc_search.calls == []
    assert doc_search.embeds == []
    assert model.calls[0]["documents"] is None
    assert model.calls[0]["available_documents"] is None
    assert "vectors" not in retrieved.calls[0]


def test_chat_search_and_document_search_share_one_query_embedding(session_factory, model, indexed, retrieved, doc_search):
    asyncio.run(collect_docs([DOC]))

    assert doc_search.embeds == ["hello"]
    assert retrieved.calls[0]["vectors"] is doc_search.calls[0]["query_vectors"]


def test_a_failing_document_search_still_gives_an_answer(session_factory, db, model, indexed, retrieved, doc_search):
    doc_search.error = RuntimeError("qdrant down")

    events = asyncio.run(collect_docs([DOC]))

    assert events[-1].answer == "Hello"
    assert model.calls[0]["documents"] is None
    assert saved_messages(db) == [("user", "hello"), ("assistant", "Hello")]


def test_citations_are_saved_with_the_answer_and_come_back_on_replay(session_factory, db, model, indexed, retrieved, doc_search):
    question = str(uuid.uuid4())
    doc_search.results = [doc_hit()]

    async def scenario():
        first = await collect_docs([DOC], user_message_id=question)
        await drain_background()
        again = await collect_docs([DOC], user_message_id=question)
        return first, again

    first, again = asyncio.run(scenario())

    assert len(model.calls) == 1  # the second request is a replay: no new answer
    assert len(doc_search.calls) == 1

    replayed = again[0].results[0]["payload"]
    assert replayed["document_id"] == DOC
    assert replayed["filename"] == "report.pdf"
    assert replayed["chunk_index"] == 2
    assert replayed["section_title"] == "Results"
    assert replayed["page"] == 3
    assert again[-1].answer == first[-1].answer


def test_context_for_storage_keeps_document_fields_only_for_excerpts():
    saved = turn.context_for_storage([hit("user", "old", "m"), doc_hit()])

    assert "document_id" not in saved[0]
    assert {key: saved[1][key] for key in turn.DOC_FIELDS} == {
        "document_id": DOC,
        "filename": "report.pdf",
        "chunk_index": 2,
        "section_title": "Results",
        "page": 3,
    }


# ---------- no document chosen: the whole library is searched ----------

BOB = "bob-id"
LIBRARY = [
    {"document_id": DOC, "filename": "report.pdf"},
    {"document_id": OTHER_DOC, "filename": "notes.md"},
]


def test_with_no_document_chosen_the_whole_library_is_searched(session_factory, model, indexed, retrieved, doc_search):
    """The question does not name a file: the right document is still found."""

    doc_search.library = LIBRARY
    doc_search.results = [doc_hit()]

    events = asyncio.run(collect(query="What does it say about context window management?"))

    call = doc_search.calls[0]
    assert call["document_ids"] is None  # None = all of this user's documents
    assert call["user_id"] == ALICE
    assert call["limit"] == turn.DOCUMENT_CONTEXT_LIMIT
    assert doc_search.embeds == ["What does it say about context window management?"]
    assert [r["id"] for r in model.calls[0]["documents"]] == ["c1"]
    assert [r["id"] for r in events[0].results] == ["c1"]


def test_nothing_is_status_checked_when_no_document_is_chosen(session_factory, model, indexed, retrieved, doc_search):
    doc_search.library = LIBRARY

    asyncio.run(collect())

    assert doc_search.state_calls == []


def test_the_library_is_looked_up_and_searched_for_the_callers_user_only(session_factory, model, indexed, retrieved, doc_search):
    doc_search.library = LIBRARY

    asyncio.run(collect(user_id=BOB))

    assert doc_search.library_calls == [BOB]
    assert doc_search.calls[0]["user_id"] == BOB


def test_the_chat_search_and_the_library_search_share_one_embedding(session_factory, model, indexed, retrieved, doc_search):
    doc_search.library = LIBRARY

    asyncio.run(collect())

    assert doc_search.embeds == ["hello"]
    assert retrieved.calls[0]["vectors"] is doc_search.calls[0]["query_vectors"]


def test_with_no_document_chosen_the_model_also_gets_the_document_names(session_factory, model, indexed, retrieved, doc_search):
    doc_search.library = LIBRARY
    doc_search.results = [doc_hit()]

    asyncio.run(collect())

    assert model.calls[0]["available_documents"] == ["report.pdf", "notes.md"]


def test_a_general_question_still_gets_the_names_when_no_excerpt_matches(session_factory, db, model, indexed, retrieved, doc_search):
    """'What are these documents about?' finds no excerpt worth sending. The
    model must still be able to list the documents instead of guessing one."""

    doc_search.library = LIBRARY
    doc_search.results = []

    events = asyncio.run(collect(query="What are these documents about?"))

    assert model.calls[0]["documents"] is None
    assert model.calls[0]["available_documents"] == ["report.pdf", "notes.md"]
    assert events[-1].answer == "Hello"


def test_a_chosen_document_skips_the_library_and_the_names(session_factory, model, indexed, retrieved, doc_search):
    doc_search.library = LIBRARY
    doc_search.results = [doc_hit()]

    asyncio.run(collect_docs([DOC]))

    assert doc_search.library_calls == []
    assert doc_search.calls[0]["document_ids"] == [DOC]
    assert model.calls[0]["available_documents"] is None


def test_if_the_library_cannot_be_listed_the_chat_still_answers(session_factory, db, model, indexed, retrieved, doc_search):
    doc_search.library_error = RuntimeError("qdrant down")

    events = asyncio.run(collect())

    assert events[-1].answer == "Hello"
    assert doc_search.calls == []
    assert model.calls[0]["documents"] is None
    assert model.calls[0]["available_documents"] is None
    assert saved_messages(db) == [("user", "hello"), ("assistant", "Hello")]


def test_if_the_library_search_fails_the_chat_still_answers_with_the_names(session_factory, db, model, indexed, retrieved, doc_search):
    doc_search.library = LIBRARY
    doc_search.error = RuntimeError("qdrant down")

    events = asyncio.run(collect())

    assert events[-1].answer == "Hello"
    assert model.calls[0]["documents"] is None
    assert model.calls[0]["available_documents"] == ["report.pdf", "notes.md"]
    assert saved_messages(db) == [("user", "hello"), ("assistant", "Hello")]


def test_the_conversation_history_still_reaches_the_model_next_to_library_excerpts(session_factory, model, indexed, retrieved, doc_search):
    doc_search.library = LIBRARY
    doc_search.results = [doc_hit()]

    asyncio.run(collect(query="first question"))
    asyncio.run(collect(query="and what about the second one?"))

    history = model.calls[1]["history"]
    assert [(t["role"], t["content"]) for t in history] == [
        ("user", "first question"),
        ("assistant", "Hello"),
    ]
    assert [r["id"] for r in model.calls[1]["documents"]] == ["c1"]


def test_library_excerpts_are_saved_with_the_answer_and_come_back_on_replay(session_factory, db, model, indexed, retrieved, doc_search):
    """The sources found without the user choosing a document are kept like
    any other: an old answer still shows them after a reload."""

    question = str(uuid.uuid4())
    doc_search.library = LIBRARY
    doc_search.results = [doc_hit()]

    async def scenario():
        async def ask():
            return [
                event
                async for event in run_turn(ALICE, CHAT, "hello", user_message_id=question)
            ]

        first = await ask()
        await drain_background()
        again = await ask()
        return first, again

    first, again = asyncio.run(scenario())

    assert len(model.calls) == 1  # the second request is a replay: no new answer
    assert len(doc_search.calls) == 1

    replayed = again[0].results[0]["payload"]
    assert replayed["document_id"] == DOC
    assert replayed["filename"] == "report.pdf"
    assert again[-1].answer == first[-1].answer
