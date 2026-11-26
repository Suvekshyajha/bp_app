"""Streaming contract: tokens flow while the model is still writing.

A gated fake model writes token N+1 only after the consumer has received
token N. That turns "it streams" into an exact statement instead of a timing
guess: if anything between the model and the consumer buffers the tokens, the
consumer waits for a token the model has not been allowed to write yet, and
the test fails with a TimeoutError instead of hanging.

The contract (see plan_streaming.md):
  1. incremental     token N arrives before the model writes token N+1
  2. context first   Context arrives before the model wrote any token
  3. complete        Context, every Token in order, exactly one Done last,
                     and the tokens joined are Done.answer
"""

import asyncio
import sys

import turn
from conftest import drain_background
from turn import Context, Done, Token, run_turn

ALICE = "alice-id"
CHAT = "chat-0001"
DOC = "11111111-1111-4111-8111-111111111111"

# How long to wait for one event. Generous for slow CI machines, but finite:
# a buffered stream shows up as a TimeoutError, never as a hung test run.
WAIT = 3


class GatedModel:
    """Stands in for llm.stream_response.

    Token i is written only after gates[i] is set. `produced` counts the
    tokens written so far. Create it inside the running event loop.
    """

    def __init__(self, tokens):
        self.tokens = tokens
        self.gates = [asyncio.Event() for _ in tokens]
        self.produced = 0

    def open_all(self):
        for gate in self.gates:
            gate.set()

    async def stream(self, query, context, history=None, documents=None, available_documents=None):
        for number, text in enumerate(self.tokens):
            await self.gates[number].wait()
            self.produced += 1
            yield text


async def next_event(events):
    return await asyncio.wait_for(events.__anext__(), WAIT)


def doc_hit():
    return {
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
    }


# ---------- the environment streaming needs ----------

def test_python_can_stream_from_async_graph_nodes():
    # On Python older than 3.11, LangGraph's get_stream_writer() does not work
    # inside async nodes (the context is not carried into the node's task), so
    # nothing would stream. CI uses 3.12.
    assert sys.version_info >= (3, 11), (
        "LangGraph's stream writer needs Python 3.11 or newer in async nodes"
    )


# ---------- contract 1 and 2: incremental, context first ----------

def test_each_token_arrives_before_the_next_one_is_written(session_factory, indexed, retrieved, monkeypatch):
    async def scenario():
        model = GatedModel(["a", "b", "c"])
        monkeypatch.setattr(turn, "stream_response", model.stream)
        events = run_turn(ALICE, CHAT, "hello")

        try:
            first = await next_event(events)
            assert isinstance(first, Context)
            assert model.produced == 0  # contract 2: before ANY token was written

            for number in range(3):
                model.gates[number].set()
                event = await next_event(events)  # TimeoutError here = the stream is buffered

                assert isinstance(event, Token)
                assert event.text == "abc"[number]
                assert model.produced == number + 1  # contract 1: the next token is not written yet

            done = await next_event(events)
            assert isinstance(done, Done)
            assert done.answer == "abc"
        finally:
            model.open_all()
            await events.aclose()
            await drain_background()

    asyncio.run(scenario())


def test_the_first_token_arrives_while_the_model_is_still_blocked(session_factory, indexed, retrieved, monkeypatch):
    # Only the first gate is opened. The model is stuck before token 2, so the
    # turn can not be finished, yet token 1 must already be in the consumer's hands.
    async def scenario():
        model = GatedModel(["first", "second"])
        monkeypatch.setattr(turn, "stream_response", model.stream)
        events = run_turn(ALICE, CHAT, "hello")

        try:
            assert isinstance(await next_event(events), Context)

            model.gates[0].set()
            token = await next_event(events)

            assert isinstance(token, Token) and token.text == "first"
            assert model.produced == 1  # still blocked before "second"
        finally:
            model.open_all()
            await events.aclose()
            await drain_background()

    asyncio.run(scenario())


def test_context_with_document_excerpts_arrives_before_the_model_writes_anything(
    session_factory, indexed, retrieved, doc_search, monkeypatch
):
    doc_search.library = [{"document_id": DOC, "filename": "report.pdf"}]
    doc_search.results = [doc_hit()]

    async def scenario():
        model = GatedModel(["a"])
        monkeypatch.setattr(turn, "stream_response", model.stream)
        events = run_turn(ALICE, CHAT, "hello")

        try:
            context = await next_event(events)

            assert isinstance(context, Context)
            assert [r["id"] for r in context.results] == ["c1"]  # the excerpt is in it
            assert model.produced == 0
        finally:
            model.open_all()
            await events.aclose()
            await drain_background()

    asyncio.run(scenario())


# ---------- contract 3: complete and ordered ----------

def test_context_then_every_token_in_order_then_exactly_one_done(session_factory, indexed, retrieved, monkeypatch):
    pieces = ["Hel", "lo ", "wor", "ld", "!"]

    async def scenario():
        model = GatedModel(pieces)
        model.open_all()  # no gating here: this test is about order and completeness
        monkeypatch.setattr(turn, "stream_response", model.stream)

        collected = [event async for event in run_turn(ALICE, CHAT, "hello")]
        await drain_background()
        return collected

    collected = asyncio.run(scenario())
    kinds = [type(event).__name__ for event in collected]

    assert kinds == ["Context"] + ["Token"] * len(pieces) + ["Done"]
    assert kinds.count("Done") == 1 and kinds[-1] == "Done"
    assert kinds.index("Context") < kinds.index("Token")
    assert [e.text for e in collected if isinstance(e, Token)] == pieces


def test_the_tokens_joined_are_the_saved_answer(session_factory, db, indexed, retrieved, monkeypatch):
    from models import Message

    async def scenario():
        model = GatedModel(["one ", "two ", "three"])
        model.open_all()
        monkeypatch.setattr(turn, "stream_response", model.stream)

        collected = [event async for event in run_turn(ALICE, CHAT, "hello")]
        await drain_background()
        return collected

    collected = asyncio.run(scenario())
    joined = "".join(e.text for e in collected if isinstance(e, Token))
    done = collected[-1]

    assert joined == done.answer == "one two three"

    db.expire_all()
    assert db.get(Message, done.assistant_message_id).content == joined  # what was streamed is what was saved