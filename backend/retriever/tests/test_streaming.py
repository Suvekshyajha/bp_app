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

The same contract is checked twice: on run_turn alone, and through the real
gRPC server with a real client stub, so anything between run_turn and the wire
(ChatServicer.Chat, grpc itself) that buffers is caught too.
"""

import asyncio
import logging
import re
import sys
import time
from contextlib import asynccontextmanager

import grpc
import groq
import httpx
import pytest

import chat_pb2
import chat_pb2_grpc
import main
import repository
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


# ---------- the real gRPC server (copied from test_grpc.py on purpose) ----------

@asynccontextmanager
async def running_server():
    server = grpc.aio.server()
    chat_pb2_grpc.add_ChatServiceServicer_to_server(main.ChatServicer(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
        yield chat_pb2_grpc.ChatServiceStub(channel)

    await server.stop(grace=1)


def grpc_request(query="hello"):
    return chat_pb2.ChatRequest(user_id=ALICE, conversation_id=CHAT, query=query)


async def next_message(messages):
    return await asyncio.wait_for(messages.__anext__(), WAIT)


# ---------- step 2: the same contract over gRPC ----------

def test_over_grpc_each_token_arrives_before_the_next_one_is_written(
    session_factory, indexed, retrieved, monkeypatch
):
    async def scenario():
        model = GatedModel(["a", "b", "c"])
        monkeypatch.setattr(turn, "stream_response", model.stream)

        async with running_server() as chat:
            call = chat.Chat(grpc_request())
            messages = call.__aiter__()

            try:
                first = await next_message(messages)
                assert first.WhichOneof("event") == "context"
                assert model.produced == 0  # contract 2, on the wire

                received = []

                for number in range(3):
                    model.gates[number].set()
                    message = await next_message(messages)  # TimeoutError = buffered somewhere

                    assert message.WhichOneof("event") == "token"
                    received.append(message.token.text)
                    assert received == list("abc"[: number + 1])
                    assert model.produced == len(received)  # contract 1, on the wire

                last = await next_message(messages)
                assert last.WhichOneof("event") == "done"
                assert last.done.answer == "abc"  # contract 3

                with pytest.raises(StopAsyncIteration):  # nothing after Done
                    await next_message(messages)
            finally:
                model.open_all()
                call.cancel()
                await drain_background()

    asyncio.run(scenario())


def test_over_grpc_the_first_token_arrives_while_the_model_is_still_blocked(
    session_factory, indexed, retrieved, monkeypatch
):
    async def scenario():
        model = GatedModel(["first", "second"])
        monkeypatch.setattr(turn, "stream_response", model.stream)

        async with running_server() as chat:
            call = chat.Chat(grpc_request())
            messages = call.__aiter__()

            try:
                assert (await next_message(messages)).WhichOneof("event") == "context"

                model.gates[0].set()
                message = await next_message(messages)

                assert message.WhichOneof("event") == "token"
                assert message.token.text == "first"
                assert model.produced == 1  # still blocked before "second"
            finally:
                model.open_all()
                call.cancel()
                await drain_background()

    asyncio.run(scenario())


# ---------- step 3: many tokens and a slow client ----------

def test_two_thousand_tokens_arrive_in_order_and_none_is_lost(
    session_factory, model, indexed, retrieved
):
    pieces = [f"t{number} " for number in range(2000)]
    model.tokens = pieces

    async def scenario():
        started = time.monotonic()
        collected = [event async for event in run_turn(ALICE, CHAT, "hello")]
        elapsed = time.monotonic() - started
        await drain_background()
        return collected, elapsed

    collected, elapsed = asyncio.run(scenario())
    kinds = [type(event).__name__ for event in collected]

    assert kinds == ["Context"] + ["Token"] * len(pieces) + ["Done"]
    assert [e.text for e in collected if isinstance(e, Token)] == pieces
    assert collected[-1].answer == "".join(pieces)

    # A generous guard, not a benchmark: it only trips if every token suddenly
    # costs real work (a checkpointer, a database write per token, ...).
    assert elapsed < 10, f"2000 tokens took {elapsed:.1f}s"


def test_a_slow_consumer_still_gets_every_token_in_order(
    session_factory, model, indexed, retrieved
):
    # LangGraph's stream queue lets the model run ahead of a slow consumer.
    # That is accepted; what must hold is that nothing is lost or reordered.
    pieces = [f"t{number} " for number in range(300)]
    model.tokens = pieces

    async def scenario():
        collected = []

        async for event in run_turn(ALICE, CHAT, "hello"):
            collected.append(event)

            if isinstance(event, Token):
                await asyncio.sleep(0.005)

        await drain_background()
        return collected

    collected = asyncio.run(scenario())
    kinds = [type(event).__name__ for event in collected]

    assert kinds == ["Context"] + ["Token"] * len(pieces) + ["Done"]
    assert [e.text for e in collected if isinstance(e, Token)] == pieces
    assert collected[-1].answer == "".join(pieces)


def test_over_grpc_a_slow_consumer_still_gets_every_token_in_order(
    session_factory, model, indexed, retrieved
):
    pieces = [f"t{number} " for number in range(300)]
    model.tokens = pieces

    async def scenario():
        messages = []

        async with running_server() as chat:
            async for message in chat.Chat(grpc_request()):
                messages.append(message)

                if message.WhichOneof("event") == "token":
                    await asyncio.sleep(0.005)

        await drain_background()
        return messages

    messages = asyncio.run(scenario())
    kinds = [message.WhichOneof("event") for message in messages]

    assert kinds == ["context"] + ["token"] * len(pieces) + ["done"]
    assert [m.token.text for m in messages if m.WhichOneof("event") == "token"] == pieces
    assert messages[-1].done.answer == "".join(pieces)


# ---------- step 4: stop and failure while tokens are flowing, over gRPC ----------

KEY = "33333333-3333-4333-8333-333333333333"


async def wait_until(condition, seconds=3):
    """Poll until condition() is true. A finite wait, so a bug is a clear failure, not a hang."""
    deadline = time.monotonic() + seconds

    while not condition():
        assert time.monotonic() < deadline, "timed out waiting for the server to finish"
        await asyncio.sleep(0.02)


def saved_messages(db):
    db.expire_all()
    rows = db.query(repository.Message).order_by(repository.Message.created_at).all()
    return [(m.role, m.content) for m in rows]


async def cancel_after_two_tokens(db, model):
    """Send a question with an idempotency key, read 2 tokens, press stop.

    Returns (tokens received, tasks still alive once the server is gone).
    """
    model.tokens = ["a", "b", "c", "d"]
    model.block_after = 2  # the model is stuck before the third token

    received = []

    async with running_server() as chat:
        call = chat.Chat(
            chat_pb2.ChatRequest(
                user_id=ALICE, conversation_id=CHAT, query="hello", user_message_id=KEY
            )
        )

        async for message in call:
            if message.WhichOneof("event") == "token":
                received.append(message.token.text)

            if len(received) == 2:
                call.cancel()
                break

        # The server notices the cancel, saves the partial answer and lets go
        await wait_until(
            lambda: model.closed
            and not turn._running
            and ("assistant", "ab") in saved_messages(db)
        )
        await drain_background()

    # The server and the channel are closed now: only this test's task may be left
    current = asyncio.current_task()
    leftover = []

    for _ in range(100):
        leftover = [task for task in asyncio.all_tasks() if task is not current]

        if not leftover:
            break

        await asyncio.sleep(0.02)

    return received, leftover


def test_cancel_after_two_tokens_saves_the_partial_answer_once_and_releases_the_claim(
    session_factory, db, model, indexed, retrieved, monkeypatch
):
    monkeypatch.setattr(turn, "delete_message_points", lambda ids: None)

    received, _ = asyncio.run(cancel_after_two_tokens(db, model))

    assert received == ["a", "b"]
    assert model.closed is True  # the model stream was stopped, not left running
    assert saved_messages(db) == [("user", "hello"), ("assistant", "ab")]  # one partial answer, once
    assert turn._running == {}  # the claim is released: the same id can be sent again now


def test_cancel_leaves_no_orphan_task_behind(session_factory, db, model, indexed, retrieved, monkeypatch):
    monkeypatch.setattr(turn, "delete_message_points", lambda ids: None)

    _, leftover = asyncio.run(cancel_after_two_tokens(db, model))

    assert leftover == [], f"tasks still running after the cancel: {leftover}"


def groq_rate_limit():
    http_request = httpx.Request("POST", "http://groq.test")
    return groq.RateLimitError("limit", response=httpx.Response(429, request=http_request), body=None)


def test_groq_failing_after_two_tokens_gives_those_tokens_then_an_error_and_saves_no_answer(
    session_factory, db, indexed, retrieved, monkeypatch
):
    monkeypatch.setattr(turn, "delete_message_points", lambda ids: None)

    async def two_tokens_then_rate_limit(query, context, history=None, documents=None, available_documents=None):
        yield "a"
        yield "b"
        raise groq_rate_limit()

    monkeypatch.setattr(turn, "stream_response", two_tokens_then_rate_limit)

    async def scenario():
        kinds = []
        tokens = []

        async with running_server() as chat:
            call = chat.Chat(
                chat_pb2.ChatRequest(
                    user_id=ALICE, conversation_id=CHAT, query="hello", user_message_id=KEY
                )
            )

            with pytest.raises(grpc.aio.AioRpcError) as error:
                async for message in call:
                    kinds.append(message.WhichOneof("event"))

                    if message.WhichOneof("event") == "token":
                        tokens.append(message.token.text)

            await wait_until(lambda: not turn._running)
            await drain_background()

        return kinds, tokens, error.value.code()

    kinds, tokens, code = asyncio.run(scenario())

    assert tokens == ["a", "b"]  # the client got what was written ...
    assert kinds == ["context", "token", "token"]  # ... and never a Done
    assert code == grpc.StatusCode.RESOURCE_EXHAUSTED
    # No half answer is saved. The question stays, so a retry with the same id works.
    assert saved_messages(db) == [("user", "hello")]
    assert turn._running == {}


# ---------- step 5: one first_token log line per turn ----------

class FirstTokenLogs(logging.Filter):
    """Collects the first_token log lines (same idea as NodeLogs in test_turn_graph.py)."""

    def __init__(self):
        super().__init__()
        self.lines = []

    def filter(self, record):
        if record.getMessage().startswith("first_token"):
            self.lines.append(record.getMessage())

        return True


@pytest.fixture
def first_token_logs():
    logs = FirstTokenLogs()
    logger = logging.getLogger("turn")
    previous = (logger.level, logger.disabled, logging.root.manager.disable)

    logging.disable(logging.NOTSET)
    logger.disabled = False
    logger.setLevel(logging.INFO)
    logger.addFilter(logs)

    yield logs

    logger.removeFilter(logs)
    logger.setLevel(previous[0])
    logger.disabled = previous[1]
    logging.disable(previous[2])


def test_a_normal_turn_logs_first_token_once_and_never_any_text(
    session_factory, model, indexed, retrieved, first_token_logs
):
    model.tokens = ["secret ", "answer"]

    async def scenario():
        collected = [event async for event in run_turn(ALICE, CHAT, "secret question")]
        await drain_background()
        return collected

    asyncio.run(scenario())

    assert len(first_token_logs.lines) == 1
    assert re.fullmatch(r"first_token ms=\d+", first_token_logs.lines[0])  # a number, no text


def test_no_first_token_line_when_the_model_fails_before_the_first_token(
    session_factory, model, indexed, retrieved, first_token_logs
):
    model.fail_after = 0  # raises before token 0

    async def scenario():
        with pytest.raises(RuntimeError):
            async for _ in run_turn(ALICE, CHAT, "hello"):
                pass

        await drain_background()

    asyncio.run(scenario())

    assert first_token_logs.lines == []


# ---------- step 8: token coalescing (optional, off by default) ----------

def test_coalescing_is_off_by_default():
    # The incremental tests above run with it off, so they prove the normal path.
    assert turn.TOKEN_COALESCE_MS == 0


def test_with_coalescing_on_the_first_token_is_never_delayed(
    session_factory, indexed, retrieved, monkeypatch
):
    monkeypatch.setattr(turn, "TOKEN_COALESCE_MS", 5000)  # a window far longer than the test

    async def scenario():
        model = GatedModel(["first", "second"])
        monkeypatch.setattr(turn, "stream_response", model.stream)
        events = run_turn(ALICE, CHAT, "hello")

        try:
            assert isinstance(await next_event(events), Context)

            model.gates[0].set()
            token = await next_event(events)  # not held for the 5 s window

            assert isinstance(token, Token) and token.text == "first"
            assert model.produced == 1
        finally:
            model.open_all()
            await events.aclose()
            await drain_background()

    asyncio.run(scenario())


def test_with_coalescing_on_a_burst_is_merged_but_nothing_is_lost_or_reordered(
    session_factory, model, indexed, retrieved, monkeypatch
):
    monkeypatch.setattr(turn, "TOKEN_COALESCE_MS", 50)
    pieces = [f"t{number} " for number in range(50)]
    model.tokens = pieces  # written back to back, with no pause at all

    async def scenario():
        collected = [event async for event in run_turn(ALICE, CHAT, "hello")]
        await drain_background()
        return collected

    collected = asyncio.run(scenario())
    tokens = [e for e in collected if isinstance(e, Token)]
    kinds = [type(event).__name__ for event in collected]

    assert kinds[0] == "Context" and kinds[-1] == "Done" and kinds.count("Done") == 1
    assert len(tokens) < len(pieces)  # fewer messages ...
    assert tokens[0].text == pieces[0]  # ... the first word still went out on its own ...
    assert "".join(e.text for e in tokens) == "".join(pieces)  # ... and no text was lost
    assert collected[-1].answer == "".join(pieces)


def test_with_coalescing_on_held_text_is_sent_by_the_timer_while_the_model_is_silent(
    session_factory, indexed, retrieved, monkeypatch
):
    monkeypatch.setattr(turn, "TOKEN_COALESCE_MS", 50)

    async def scenario():
        model = GatedModel(["a", "b", "c"])
        monkeypatch.setattr(turn, "stream_response", model.stream)
        events = run_turn(ALICE, CHAT, "hello")
        received = []

        try:
            assert isinstance(await next_event(events), Context)

            model.gates[0].set()
            received.append((await next_event(events)).text)  # "a", at once

            model.gates[1].set()  # "b" comes right after "a": it is held ...
            received.append((await next_event(events)).text)  # ... and the TIMER sends it
            assert received == ["a", "b"]
            assert model.produced == 2  # the model is still blocked before "c"

            model.gates[2].set()
            rest = [event async for event in events]
        finally:
            model.open_all()
            await events.aclose()
            await drain_background()

        return received, rest

    received, rest = asyncio.run(scenario())

    assert "".join(received) + "".join(e.text for e in rest if isinstance(e, Token)) == "abc"
    assert isinstance(rest[-1], Done) and rest[-1].answer == "abc"


def test_with_coalescing_on_stopping_still_saves_the_held_text(
    session_factory, db, indexed, retrieved, monkeypatch
):
    monkeypatch.setattr(turn, "TOKEN_COALESCE_MS", 5000)  # the timer will not fire in this test

    async def scenario():
        model = GatedModel(["a", "b", "c"])
        monkeypatch.setattr(turn, "stream_response", model.stream)

        async with running_server() as chat:
            call = chat.Chat(grpc_request())
            messages = call.__aiter__()

            try:
                assert (await next_message(messages)).WhichOneof("event") == "context"

                model.gates[0].set()
                assert (await next_message(messages)).token.text == "a"

                model.gates[1].set()  # "b" is written by the model, but held (5 s window)
                await wait_until(lambda: model.produced == 2)

                call.cancel()  # the stop button

                await wait_until(lambda: ("assistant", "ab") in saved_messages(db))
                await drain_background()
            finally:
                model.open_all()
                call.cancel()

    asyncio.run(scenario())

    assert saved_messages(db) == [("user", "hello"), ("assistant", "ab")]