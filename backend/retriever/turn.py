"""One chat turn, from the question to the saved answer.

`run_turn` is a plain async generator with no gRPC in it: it yields what
happened (the retrieved context, each token, the final summary) and raises
if something goes wrong. main.py turns those into gRPC messages and gRPC
status codes. That keeps this file easy to test.

Stopping: if the caller goes away (the user pressed stop, or the browser
closed) the generator is cancelled. Whatever was written so far is still
saved, because the saving runs shielded in the `finally` block.
"""

import asyncio
import functools
import logging
import os
import time
import types
import uuid
from contextlib import aclosing
from dataclasses import dataclass
from typing import TypedDict

import doc_retrieval
import repository
from common.sanitize import find_injection
from embeddings import embed_query
from llm import stream_response
from qdrant_store import delete_message_points, store_message
from repository import ConversationNotFound
from retrieval import retrieve_messages


logger = logging.getLogger(__name__)

# How many related older messages of this chat to add on top of the history
RETRIEVAL_LIMIT = 5

# How many document excerpts one question may use. The chunks next to the best
# ones come on top of this (see doc_retrieval.NEIGHBOR_HITS); the prompt
# budget in llm.py decides how many of them the model actually sees.
DOCUMENT_CONTEXT_LIMIT = 6

# Token coalescing (optional, OFF by default). 0 = every token goes out the moment
# the model writes it. With a value such as 25, tokens that arrive faster than one
# per 25 ms are sent together, which lowers the message rate. The first token is
# never delayed. Turn it on only if the message rate is a measured problem.
TOKEN_COALESCE_MS = int(os.getenv("TOKEN_COALESCE_MS", "0"))

# What is kept (and sent to the client) for a document excerpt, next to the
# usual id / score / role / content / timestamp.
DOC_FIELDS = ("document_id", "filename", "chunk_index", "section_title", "page")


class DocumentNotFound(Exception):
    """A document id does not exist or belongs to someone else (the same
    answer for both, so ids cannot be probed)."""


class DocumentNotReady(Exception):
    """A document is still being processed by the ingestion worker."""

# Embedding is CPU-heavy: never run more than this many at the same time.
# Only embedding and searching hold it, never a whole LLM answer.
MAX_CONCURRENT_JOBS = 4
_jobs = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

# Keeps background tasks alive until they finish
_background: set[asyncio.Task] = set()


# ---------- What a turn yields ----------

@dataclass
class Context:
    results: list[dict]  # [{"id", "score", "payload": {...}}], best first


@dataclass
class Token:
    text: str


@dataclass
class Done:
    user_message_id: str
    assistant_message_id: str
    answer: str


# ---------- Helpers ----------

def drop_messages_already_in_history(
    results: list[dict],
    history: list[dict],
) -> list[dict]:
    """Remove retrieved messages the model already gets as chat history.

    Retrieval searches the same chat as the history, so the most similar
    messages are often the recent ones. Sending them twice wastes context.
    """
    seen = {(turn["role"], turn["content"]) for turn in history}

    return [
        item
        for item in results
        if (
            item.get("payload", {}).get("role"),
            item.get("payload", {}).get("content"),
        ) not in seen
    ]


def _spawn(coroutine) -> asyncio.Task:
    task = asyncio.create_task(coroutine)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def _index(
    user_id: str,
    conversation_id: str,
    role: str,
    content: str,
    message_id: str,
) -> None:
    """Add a message to Qdrant. SQLite is the source of truth, so a failure
    here is logged and never shown as a chat error."""

    try:
        async with _jobs:
            # The chat may have been deleted while this waited for its turn:
            # do not bring its text back into the index.
            if not await asyncio.to_thread(
                repository.conversation_exists, conversation_id
            ):
                return

            await asyncio.to_thread(
                store_message,
                user_id=user_id,
                conversation_id=conversation_id,
                role=role,
                content=content,
                message_id=message_id,
            )
    except Exception:
        logger.exception("Could not index the %s message in Qdrant", role)


async def _retrieve(
    query: str,
    user_id: str,
    conversation_id: str,
    limit: int,
    vectors: dict | None = None,
) -> list[dict]:
    """Related older messages. If the search is down the chat still works
    from its history, just without the extra context."""

    # Only passed when the question was already embedded for the document search
    shared = {"vectors": vectors} if vectors is not None else {}

    try:
        async with _jobs:
            return await asyncio.to_thread(
                retrieve_messages,
                query=query,
                user_id=user_id,
                conversation_id=conversation_id,
                limit=limit,
                **shared,
            )
    except Exception:
        logger.exception("Retrieval failed; answering without it")
        return []


async def _embed(query: str) -> dict | None:
    """Embed the question once, for the chat search AND the document search.

    If the embedding fails the searches simply find nothing: the chat still
    answers from its history.
    """

    try:
        async with _jobs:
            return await asyncio.to_thread(embed_query, query)
    except Exception:
        logger.exception("Could not embed the question; answering without search")
        return None


async def _check_documents(user_id: str, document_ids: list[str]) -> None:
    """Every document must exist, belong to this user and be ready.

    Runs before anything is saved, so the same message can be sent again later.
    A missing document is reported first: it must not reveal whether someone
    else's document exists (the user id is part of every lookup).
    """

    states = await asyncio.to_thread(
        doc_retrieval.document_states, user_id, document_ids
    )

    if any(states.get(document_id, "missing") == "missing" for document_id in document_ids):
        raise DocumentNotFound()

    if any(states.get(document_id) == "processing" for document_id in document_ids):
        raise DocumentNotReady()


async def _list_library(user_id: str) -> list[dict]:
    """The user's finished documents: [{"document_id", "filename"}].

    Used when the user chose no document. A failure is logged and means "no
    documents": the chat still answers, as it did before documents existed.
    """

    try:
        async with _jobs:
            return await asyncio.to_thread(doc_retrieval.list_documents, user_id)
    except Exception:
        logger.exception("Could not list the documents; answering without them")
        return []


async def _retrieve_documents(
    vectors: dict | None,
    user_id: str,
    document_ids: list[str] | None,
    limit: int,
) -> list[dict]:
    """The best excerpts of the chosen documents, or of ALL the user's documents
    when `document_ids` is None. A failing search must not stop the chat, so it
    is logged and the answer is written without it."""

    if vectors is None:
        return []

    try:
        async with _jobs:
            return await asyncio.to_thread(
                doc_retrieval.retrieve_document_chunks,
                query_vectors=vectors,
                user_id=user_id,
                document_ids=document_ids,
                limit=limit,
            )
    except Exception:
        logger.exception("Document search failed; answering without it")
        return []


def context_for_storage(results: list[dict]) -> list[dict]:
    """The related messages and document excerpts in the shape kept with the answer."""

    saved = []

    for item in results:
        payload = item.get("payload") or {}

        entry = {
            "id": item["id"],
            "score": item["score"],
            "role": payload.get("role"),
            "content": payload.get("content", ""),
            "timestamp": payload.get("timestamp"),
        }

        # Excerpts keep their file name and place, so the sources of an old
        # answer still show after a reload
        if payload.get("document_id"):
            for key in DOC_FIELDS:
                entry[key] = payload.get(key)

        saved.append(entry)

    return saved



async def _save_answer(
    user_id: str,
    conversation_id: str,
    answer: str,
    results: list[dict],
    message_id: str | None = None,
) -> str:
    """Save the answer (with the context it used), then index it in the background."""

    if not answer:
        return ""

    message_id = await asyncio.to_thread(
        repository.add_message,
        conversation_id,
        "assistant",
        answer,
        context_for_storage(results),
        message_id,
    )
    _spawn(_index(user_id, conversation_id, "assistant", answer, message_id))

    return message_id


# ---------- The turn ----------

async def _cut_from(user_id: str, conversation_id: str, message_id: str) -> None:
    """Delete one message and everything after it (SQLite and the search index).

    Used by edit, regenerate and retry. If the message is not in this chat
    (a brand-new question) there is nothing to delete.
    """

    ids = await asyncio.to_thread(
        repository.message_ids_from, user_id, conversation_id, message_id
    )

    if not ids:
        return

    # Search index first, like delete_conversation: if it fails nothing is
    # deleted and the same request can simply be sent again.
    await asyncio.to_thread(delete_message_points, ids)
    await asyncio.to_thread(repository.delete_messages, conversation_id, ids)


class TurnInProgress(Exception):
    """The same question is already being answered and did not finish in time."""


# Questions being answered right now, by (chat id, question id). A second
# request for the same question waits here instead of starting another answer.
# (One process only. With several service processes, use a database lock.)
_running: dict[tuple[str, str], asyncio.Event] = {}
CLAIM_WAIT_SECONDS = 60


async def _claim(key: tuple[str, str]) -> None:
    """Become the only turn running for this question, waiting if needed."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + CLAIM_WAIT_SECONDS

    while key in _running:
        remaining = deadline - loop.time()

        if remaining <= 0:
            raise TurnInProgress(key[1])

        try:
            await asyncio.wait_for(_running[key].wait(), remaining)
        except asyncio.TimeoutError:
            raise TurnInProgress(key[1])

    # No await between the check above and this line, so two requests
    # can never both get here.
    _running[key] = asyncio.Event()


def _release(key: tuple[str, str]) -> None:
    event = _running.pop(key, None)

    if event is not None:
        event.set()  # wake up whoever was waiting for this question


def answer_id_for(user_message_id: str) -> str:
    """The same question always gets the same answer id, so saving an
    answer twice can never leave two answers."""

    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"{user_message_id}:assistant"))


def _results_from_saved(items: list[dict]) -> list[dict]:
    """Saved related messages and excerpts, back in the shape a Context event carries."""

    results = []

    for item in items:
        if not isinstance(item, dict):
            continue

        payload = {
            "role": item.get("role"),
            "content": item.get("content", ""),
            "timestamp": item.get("timestamp"),
        }

        if item.get("document_id"):
            for key in DOC_FIELDS:
                payload[key] = item.get(key)

        results.append({
            "id": item.get("id", ""),
            "score": item.get("score", 0.0),
            "payload": payload,
        })

    return results


def _replay(user_message_id: str, answer: dict) -> list:
    """The events of a turn that already happened, with no model call."""

    return [
        Context(_results_from_saved(answer["context"])),
        Token(answer["content"]),
        Done(
            user_message_id=user_message_id,
            assistant_message_id=answer["id"],
            answer=answer["content"],
        ),
    ]


# ---------- Graph state and nodes ----------
#
# Not used by run_turn yet. Each node takes the state and returns only the keys
# it changes. They reuse the helpers above unchanged and stay in THIS module on
# purpose: the tests replace names such as `stream_response`, `retrieve_messages`
# and `doc_retrieval.*` here, and a node must see the replacement at call time.

def _logged(name: str):
    """One log line per node: its name and how long it took.

    Never logs the question, the answer or any document text. The line is
    written in `finally`, so a failed or stopped node (the stop button) is
    logged too. The request id comes from the logging context that main.py
    sets; LangGraph runs each node in a task that copies that context.
    """

    def wrap(node):
        @functools.wraps(node)
        async def timed(state):
            started = time.monotonic()

            try:
                return await node(state)
            finally:
                logger.info(
                    "node=%s ms=%d", name, (time.monotonic() - started) * 1000
                )

        return timed

    return wrap


class TurnState(TypedDict, total=False):
    # inputs
    user_id: str
    conversation_id: str
    query: str
    user_message_id: str | None
    regenerate: bool
    document_ids: list[str]
    # working data
    saved: dict | None  # what repository.get_turn_state found (for the replay check)
    history: list[dict]
    library: list[dict]
    vectors: dict | None
    chat_results: list[dict]
    doc_results: list[dict]
    results: list[dict]  # chat_results + doc_results
    injection_found: bool  # set by check_input
    # outputs
    assistant_message_id: str
    answer: str


@_logged("check_replay")
async def node_check_replay(state: TurnState) -> dict:
    """Look up what is already saved for this question id (the idempotency check).

    Skipped (saved = None) when the browser sent no id or asked to regenerate.
    """

    user_message_id = state.get("user_message_id")

    if not user_message_id or state.get("regenerate"):
        return {"saved": None}

    saved = await asyncio.to_thread(
        repository.get_turn_state,
        state["user_id"],
        state["conversation_id"],
        user_message_id,
    )

    return {"saved": saved}


@_logged("cut_old")
async def node_cut_old(state: TurnState) -> dict:
    """Edit / regenerate / retry: remove the old question and everything after it."""

    user_message_id = state.get("user_message_id")

    if user_message_id:
        await _cut_from(state["user_id"], state["conversation_id"], user_message_id)

    return {}


@_logged("load_history")
async def node_load_history(state: TurnState) -> dict:
    """What was said earlier in THIS chat (before the new question is saved)."""

    history = await asyncio.to_thread(
        repository.load_chat_history, state["conversation_id"]
    )

    return {"history": history}


@_logged("plan_search")
async def node_plan_search(state: TurnState) -> dict:
    """List the user's documents (only if none were chosen) and embed the question once.

    With no documents chosen and none in the library there is nothing to embed
    for a document search, so `vectors` stays None, exactly as before.
    """

    document_ids = state.get("document_ids") or []
    library: list[dict] = []

    if not document_ids:
        library = await _list_library(state["user_id"])

    vectors = await _embed(state["query"]) if (document_ids or library) else None

    return {"library": library, "vectors": vectors}


@_logged("retrieve_chat")
async def node_retrieve_chat(state: TurnState) -> dict:
    """Related older messages of this chat, without the ones already in the history."""

    history = state.get("history") or []

    found = await _retrieve(
        state["query"],
        state["user_id"],
        state["conversation_id"],
        RETRIEVAL_LIMIT + len(history),
        state.get("vectors"),
    )

    return {
        "chat_results": drop_messages_already_in_history(found, history)[:RETRIEVAL_LIMIT]
    }


@_logged("retrieve_docs")
async def node_retrieve_docs(state: TurnState) -> dict:
    """Excerpts of the chosen documents, or of the whole library when none were chosen."""

    document_ids = state.get("document_ids") or []
    doc_results: list[dict] = []

    if document_ids:
        doc_results = await _retrieve_documents(
            state.get("vectors"), state["user_id"], list(document_ids), DOCUMENT_CONTEXT_LIMIT
        )
    elif state.get("library"):
        doc_results = await _retrieve_documents(
            state.get("vectors"), state["user_id"], None, DOCUMENT_CONTEXT_LIMIT
        )

    return {"doc_results": doc_results}


# ---------- Event nodes, routing and the generate node ----------
#
# These send events (Context, Token, Done) through LangGraph's custom stream
# instead of `yield`. Events never go into the state.

def _writer():
    """The function that sends one event to the caller of graph.astream().

    The import is inside the function so this module still loads where
    langgraph is not installed yet. Tests replace this name with a fake.
    """

    from langgraph.config import get_stream_writer

    return get_stream_writer()


def route_after_check(state: TurnState) -> str:
    """"replay" when the same question (same id, same text) was already answered."""

    saved = state.get("saved")

    if saved and saved["answer"] and saved["query"] == state["query"]:
        return "replay"

    return "fresh"


# What the user sees when the question is refused (see node_refuse)
REFUSAL_TEXT = (
    "I can't follow requests that try to change my rules or show my hidden "
    "instructions. Please ask your question in a different way."
)


@_logged("check_input")
async def node_check_input(state: TurnState) -> dict:
    """Look at the new question for a prompt-injection phrase.

    Only sets a flag; route_after_input decides what happens next. The
    question text is never logged, only the fact that it was refused.
    """

    found = find_injection(state["query"]) is not None

    if found:
        logger.warning("Question refused: it looks like a prompt injection")

    return {"injection_found": found}


def route_after_input(state: TurnState) -> str:
    """"refuse" when check_input found an injection phrase, else "continue"."""

    return "refuse" if state.get("injection_found") else "continue"


@_logged("refuse")
async def node_refuse(state: TurnState) -> dict:
    """Answer with a fixed refusal. No search, no model call, nothing saved.

    The events keep the usual shape (Context, Token, Done), so main.py and
    the orchestrator need no change. The question is not saved either, so it
    can not come back later as history or as a search result.
    """

    write = _writer()

    write(Context([]))
    write(Token(REFUSAL_TEXT))
    write(
        Done(
            user_message_id=state.get("user_message_id") or "",
            assistant_message_id="",
            answer=REFUSAL_TEXT,
        )
    )

    return {"answer": REFUSAL_TEXT}


@_logged("replay")
async def node_replay(state: TurnState) -> dict:
    """Send the saved answer again: no model call, nothing new saved."""

    write = _writer()

    for event in _replay(state["user_message_id"], state["saved"]["answer"]):
        write(event)

    return {}


@_logged("emit_context")
async def node_emit_context(state: TurnState) -> dict:
    """Send the Context event (chat messages first, then document excerpts)."""

    results = (state.get("chat_results") or []) + (state.get("doc_results") or [])

    _writer()(Context(results))

    return {"results": results}


@_logged("save_question")
async def node_save_question(state: TurnState) -> dict:
    """Save the question, then index it in the background.

    Runs after both searches, so the question cannot retrieve itself.
    """

    user_message_id = state.get("user_message_id")
    extra = {"message_id": user_message_id} if user_message_id else {}

    user_message_id = await asyncio.to_thread(
        repository.add_message,
        state["conversation_id"],
        "user",
        state["query"],
        **extra,
    )
    _spawn(
        _index(
            state["user_id"],
            state["conversation_id"],
            "user",
            state["query"],
            user_message_id,
        )
    )

    return {"user_message_id": user_message_id}


class _TokenSender:
    """Sends tokens to the caller, optionally merging bursts (TOKEN_COALESCE_MS).

    Window 0 (the default): every token is written at once, exactly as before.

    Window above 0: the FIRST token, and any token that follows a quiet moment,
    goes out at once. Tokens that arrive faster than one per window are held
    and sent together. Held text never waits for the next token: a timer sends
    it when the window ends, and flush() sends it at the end of the answer, when
    the turn is stopped, or when the model fails.
    """

    def __init__(self, write, window: float, loop):
        self._write = write
        self._window = window
        self._loop = loop
        self._held = ""
        self._timer = None
        self._last = None  # when something was last sent

    def add(self, text: str) -> None:
        if self._window <= 0:
            self._write(Token(text))
            return

        now = self._loop.time()

        if not self._held and (self._last is None or now - self._last >= self._window):
            self._send(text, now)
            return

        self._held += text

        if self._timer is None:
            wait = max(0.0, self._last + self._window - now)
            self._timer = self._loop.call_later(wait, self.flush)

    def flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

        if self._held:
            text, self._held = self._held, ""
            self._send(text, self._loop.time())

    def _send(self, text: str, now: float) -> None:
        self._last = now
        self._write(Token(text))


@_logged("generate")
async def node_generate(state: TurnState) -> dict:
    """Stream the answer, save it, send Done.

    Streaming and saving are ONE node on purpose: if the user presses stop the
    graph is cancelled, and a node placed after this one would never run. The
    save sits in `finally`, shielded, so a stop still keeps what was written.
    A failed model call saves nothing.
    """

    write = _writer()
    started = time.monotonic()  # for the first_token log line

    user_id = state["user_id"]
    conversation_id = state["conversation_id"]
    user_message_id = state["user_message_id"]
    chat_results = state.get("chat_results") or []
    doc_results = state.get("doc_results") or []
    library = state.get("library") or []

    # `documents` is only passed when there are excerpts. With no document
    # chosen the model also gets the NAMES of the user's documents.
    extra_args = {"documents": doc_results} if doc_results else {}

    if library:
        extra_args["available_documents"] = [d["filename"] for d in library]

    # Chat messages and document excerpts go in separate arguments
    tokens = stream_response(
        state["query"], chat_results, state.get("history") or [], **extra_args
    )

    parts: list[str] = []
    failed = False
    saving: asyncio.Future | None = None
    first_token_logged = False
    sender = _TokenSender(write, TOKEN_COALESCE_MS / 1000, asyncio.get_running_loop())

    try:
        async for text in tokens:
            if not first_token_logged:
                # Once per turn, and never any text: only how long the first word took
                first_token_logged = True
                logger.info("first_token ms=%d", (time.monotonic() - started) * 1000)

            parts.append(text)
            sender.add(text)

    except Exception:
        # The model or the network failed: nothing half-written is saved
        failed = True
        raise

    finally:
        sender.flush()  # held text (coalescing on) goes out before Done, or before the error
        await tokens.aclose()

        # Runs on normal completion AND when the caller cancelled. Shielded, so
        # a second cancel cannot interrupt the save.
        if not failed:
            saving = asyncio.ensure_future(
                _save_answer(
                    user_id,
                    conversation_id,
                    "".join(parts),
                    chat_results + doc_results,  # citations are saved with the answer
                    answer_id_for(user_message_id),
                )
            )
            await asyncio.shield(saving)

    answer = "".join(parts)
    assistant_message_id = saving.result() if saving else ""

    write(
        Done(
            user_message_id=user_message_id,
            assistant_message_id=assistant_message_id,
            answer=answer,
        )
    )

    return {"answer": answer, "assistant_message_id": assistant_message_id}


def _graph_nodes() -> types.SimpleNamespace:
    """This module's graph pieces, handed to turn_graph.get_graph().

    Taken from this module's own globals on purpose (not `import turn`): the
    nodes must be the ones that live next to the patched names.
    """

    names = globals()

    return types.SimpleNamespace(
        TurnState=names["TurnState"],
        route_after_check=names["route_after_check"],
        route_after_input=names["route_after_input"],
        **{name: fn for name, fn in names.items() if name.startswith("node_")},
    )


async def run_turn(
    user_id: str,
    conversation_id: str,
    query: str,
    user_message_id: str | None = None,
    regenerate: bool = False,
    document_ids=(),
):
    """Yield Context, then Token (many), then Done. Raise on failure.

    `document_ids` are the documents the user chose to search besides the
    chat. They are checked first: DocumentNotFound (missing or someone else's)
    or DocumentNotReady (still processing) is raised before anything is
    claimed, saved or sent to the model. When it is empty, ALL the user's
    finished documents are searched instead (nobody else's, ever).

    `user_message_id` is the idempotency key: the id the browser chose for
    this question. Sending the same question with the same id again is safe:

      - Already answered, same text:  the saved answer is replayed. No model
        call, nothing new saved.
      - Still being answered:         waits for that turn, then replays it
        (TurnInProgress if it takes longer than CLAIM_WAIT_SECONDS).
      - Saved but never answered (the model failed), or the text changed
        (edit), or `regenerate` is True:  that message and everything after
        it are deleted and the question is answered again.
    """

    # The chat must exist and belong to this user
    allowed = await asyncio.to_thread(
        repository.get_or_create_conversation,
        user_id,
        conversation_id,
        query,
    )

    if not allowed:
        raise ConversationNotFound(conversation_id)

    document_ids = list(dict.fromkeys(document_ids or ()))

    if document_ids:
        await _check_documents(user_id, document_ids)

    key = (conversation_id, user_message_id) if user_message_id else None

    if key:
        await _claim(key)

    try:
        # Imported here, not at the top: turn_graph imports this module
        from turn_graph import get_graph

        state = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "query": query,
            "user_message_id": user_message_id,
            "regenerate": regenerate,
            "document_ids": document_ids,
        }

        async with aclosing(
            get_graph(_graph_nodes()).astream(state, stream_mode="custom")
        ) as events:
            async for event in events:
                yield event

    finally:
        if key:
            _release(key)