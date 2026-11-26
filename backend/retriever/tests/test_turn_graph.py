"""The LangGraph version of the chat turn: its shape, its routing, its nodes.

The behaviour of a whole turn is covered by test_turn.py / test_grpc.py. This
file checks the graph itself, so a wrong edge or a node that moved out of
turn.py is found here with a clear message.
"""

import asyncio

import pytest

import repository
import turn
import turn_graph
from conftest import drain_background
from models import Message
from turn import Token, Done, run_turn

ALICE = "alice-id"
CHAT = "chat-0001"
QUESTION_ID = "question-0001"

NODES = {
    "check_replay", "replay", "cut_old", "load_history", "plan_search",
    "retrieve_chat", "retrieve_docs", "emit_context", "save_question", "generate",
}


# ---------- 1. the shape of the graph ----------

def test_the_graph_has_exactly_the_expected_nodes():
    drawn = turn_graph.get_graph().get_graph()

    assert set(drawn.nodes) - {"__start__", "__end__"} == NODES


def test_the_graph_has_exactly_the_expected_edges():
    drawn = turn_graph.get_graph().get_graph()

    edges = {(e.source, e.target, bool(e.conditional)) for e in drawn.edges}

    assert edges == {
        ("__start__", "check_replay", False),
        ("check_replay", "replay", True),
        ("check_replay", "cut_old", True),
        ("replay", "__end__", False),
        ("cut_old", "load_history", False),
        ("load_history", "plan_search", False),
        ("plan_search", "retrieve_chat", False),
        ("retrieve_chat", "retrieve_docs", False),
        ("retrieve_docs", "emit_context", False),
        ("emit_context", "save_question", False),
        ("save_question", "generate", False),
        ("generate", "__end__", False),
    }


def test_the_question_is_saved_after_both_searches_and_before_the_answer():
    path = turn_graph.FRESH_PATH

    assert path.index("retrieve_chat") < path.index("save_question")
    assert path.index("retrieve_docs") < path.index("save_question")
    assert path.index("emit_context") < path.index("save_question")
    assert path.index("save_question") < path.index("generate")


# ---------- 2. routing ----------

def answered(query="hello"):
    return {"query": query, "answer": {"id": "a1", "content": "Hi", "context": []}}


@pytest.mark.parametrize(
    "saved, query, expected",
    [
        (None, "hello", "fresh"),                                  # never seen this id
        ({"query": "hello", "answer": None}, "hello", "fresh"),    # saved, never answered
        (answered("hello"), "something else", "fresh"),            # answered, but the text changed (edit)
        (answered("hello"), "hello", "replay"),                    # answered, same text
    ],
    ids=["no-saved-state", "saved-but-unanswered", "answered-text-changed", "answered-same-text"],
)
def test_route_after_check(saved, query, expected):
    assert turn.route_after_check({"saved": saved, "query": query}) == expected


def test_route_after_check_with_no_saved_key_at_all():
    assert turn.route_after_check({"query": "hello"}) == "fresh"


# ---------- 3. nodes on their own ----------

def test_plan_search_does_not_embed_when_there_are_no_documents(session_factory, doc_search):
    state = {"user_id": ALICE, "query": "hello", "document_ids": []}

    update = asyncio.run(turn.node_plan_search(state))

    assert update == {"library": [], "vectors": None}
    assert doc_search.library_calls == [ALICE]  # it did look at the library...
    assert doc_search.embeds == []              # ...and found nothing to embed for


def test_plan_search_embeds_once_when_the_library_has_documents(session_factory, doc_search):
    doc_search.library = [{"document_id": "d1", "filename": "a.pdf"}]
    state = {"user_id": ALICE, "query": "hello", "document_ids": []}

    update = asyncio.run(turn.node_plan_search(state))

    assert doc_search.embeds == ["hello"]
    assert update["library"] == doc_search.library
    assert update["vectors"] is not None


def test_plan_search_with_chosen_documents_skips_the_library(session_factory, doc_search):
    state = {"user_id": ALICE, "query": "hello", "document_ids": ["d1"]}

    update = asyncio.run(turn.node_plan_search(state))

    assert doc_search.library_calls == []
    assert doc_search.embeds == ["hello"]
    assert update["library"] == []


def hit(message_id, role, content):
    return {"id": message_id, "score": 0.9, "payload": {"role": role, "content": content, "timestamp": None}}


def test_retrieve_chat_drops_messages_already_in_the_history(retrieved):
    retrieved.results = [
        hit("m1", "user", "already in history"),
        hit("m2", "user", "an older message"),
    ]
    state = {
        "user_id": ALICE,
        "conversation_id": CHAT,
        "query": "hello",
        "history": [{"role": "user", "content": "already in history"}],
        "vectors": None,
    }

    update = asyncio.run(turn.node_retrieve_chat(state))

    assert [item["id"] for item in update["chat_results"]] == ["m2"]
    # It asked for extra, because the ones in the history are removed again
    assert retrieved.calls[0]["limit"] == turn.RETRIEVAL_LIMIT + 1


def test_retrieve_chat_keeps_at_most_the_retrieval_limit(retrieved):
    retrieved.results = [hit(f"m{i}", "user", f"older {i}") for i in range(turn.RETRIEVAL_LIMIT + 3)]
    state = {"user_id": ALICE, "conversation_id": CHAT, "query": "hello", "history": [], "vectors": None}

    update = asyncio.run(turn.node_retrieve_chat(state))

    assert len(update["chat_results"]) == turn.RETRIEVAL_LIMIT


def test_emit_context_sends_chat_messages_first_then_document_excerpts(monkeypatch):
    sent = []
    monkeypatch.setattr(turn, "_writer", lambda: sent.append)
    chat = [hit("m1", "user", "older")]
    docs = [hit("d1", None, "an excerpt")]

    update = asyncio.run(turn.node_emit_context({"chat_results": chat, "doc_results": docs}))

    assert sent == [turn.Context(chat + docs)]
    assert update == {"results": chat + docs}


# ---------- 4. the generate node ----------

@pytest.fixture
def events(monkeypatch):
    """What the nodes send: replaces the LangGraph stream writer with a list."""

    sent = []
    monkeypatch.setattr(turn, "_writer", lambda: sent.append)
    return sent


def generate_state():
    repository.get_or_create_conversation(ALICE, CHAT, "hello")
    repository.add_message(CHAT, "user", "hello", message_id=QUESTION_ID)

    return {
        "user_id": ALICE,
        "conversation_id": CHAT,
        "query": "hello",
        "user_message_id": QUESTION_ID,
        "history": [],
        "chat_results": [],
        "doc_results": [],
        "library": [],
    }


def assistant_rows(db):
    db.expire_all()
    return db.query(Message).filter(Message.role == "assistant").all()


def test_generate_streams_saves_and_ends_with_done(session_factory, db, model, indexed, events):
    state = generate_state()

    update = asyncio.run(turn.node_generate(state))

    assert [e.text for e in events if isinstance(e, Token)] == ["Hel", "lo"]
    assert isinstance(events[-1], Done)
    assert update["answer"] == "Hello"
    assert update["assistant_message_id"] == turn.answer_id_for(QUESTION_ID)
    assert [m.content for m in assistant_rows(db)] == ["Hello"]


def test_generate_failure_saves_nothing_and_sends_no_done(session_factory, db, model, indexed, events):
    model.tokens = ["a", "b", "c"]
    model.fail_after = 1
    state = generate_state()

    with pytest.raises(RuntimeError):
        asyncio.run(turn.node_generate(state))

    assert assistant_rows(db) == []
    assert not any(isinstance(e, Done) for e in events)
    assert model.closed is True


def test_generate_cancelled_mid_stream_saves_the_partial_answer_once(session_factory, db, model, indexed, events):
    model.tokens = ["a", "b", "c", "d"]
    model.block_after = 2  # writes "a", "b", then waits until it is stopped
    state = generate_state()

    async def scenario():
        task = asyncio.create_task(turn.node_generate(state))

        while len([e for e in events if isinstance(e, Token)]) < 2:
            await asyncio.sleep(0.01)

        task.cancel()  # the stop button

        with pytest.raises(asyncio.CancelledError):
            await task

        await drain_background()

    asyncio.run(scenario())

    rows = assistant_rows(db)
    assert [m.content for m in rows] == ["ab"]            # saved exactly once
    assert rows[0].id == turn.answer_id_for(QUESTION_ID)
    assert model.closed is True
    assert not any(isinstance(e, Done) for e in events)   # a stopped turn never says "done"


def test_generate_cancelled_before_any_token_saves_no_empty_answer(session_factory, db, model, indexed, events):
    model.block_after = 0
    state = generate_state()

    async def scenario():
        task = asyncio.create_task(turn.node_generate(state))

        while not model.calls:
            await asyncio.sleep(0.01)

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        await drain_background()

    asyncio.run(scenario())

    assert assistant_rows(db) == []


# ---------- 5. compiled once ----------

def test_the_graph_is_compiled_once():
    assert turn_graph.get_graph() is turn_graph.get_graph()


def test_run_turn_uses_the_same_compiled_graph_as_get_graph():
    assert turn_graph.get_graph(turn._graph_nodes()) is turn_graph.get_graph()


# ---------- 6. chat results and document excerpts stay apart ----------

def test_run_turn_gives_the_model_chat_results_and_document_excerpts_separately(
    session_factory, model, indexed, retrieved, doc_search
):
    chat_hit = hit("m1", "user", "an older message")
    doc_hit = {
        "id": "chunk-1",
        "score": 0.8,
        "payload": {
            "role": None, "content": "from the file", "timestamp": None,
            "document_id": "d1", "filename": "a.pdf", "chunk_index": 0,
            "section_title": "Intro", "page": 1,
        },
    }
    retrieved.results = [chat_hit]
    doc_search.library = [{"document_id": "d1", "filename": "a.pdf"}]
    doc_search.results = [doc_hit]

    async def scenario():
        events = [event async for event in run_turn(ALICE, CHAT, "hello")]
        await drain_background()
        return events

    events = asyncio.run(scenario())

    call = model.calls[0]
    assert call["context"] == [chat_hit]            # chat messages only
    assert call["documents"] == [doc_hit]           # excerpts in their own argument
    assert call["available_documents"] == ["a.pdf"]

    # The client still gets both in ONE Context event, chat first
    assert events[0].results == [chat_hit, doc_hit]