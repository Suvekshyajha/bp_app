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
import logging
from dataclasses import dataclass

import repository
from llm import stream_response
from qdrant_store import store_message
from repository import ConversationNotFound
from retrieval import retrieve_messages


logger = logging.getLogger(__name__)

# How many related older messages of this chat to add on top of the history
RETRIEVAL_LIMIT = 5

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
) -> list[dict]:
    """Related older messages. If the search is down the chat still works
    from its history, just without the extra context."""

    try:
        async with _jobs:
            return await asyncio.to_thread(
                retrieve_messages,
                query=query,
                user_id=user_id,
                conversation_id=conversation_id,
                limit=limit,
            )
    except Exception:
        logger.exception("Retrieval failed; answering without it")
        return []


def context_for_storage(results: list[dict]) -> list[dict]:
    """The related messages in the shape kept with the answer."""

    saved = []

    for item in results:
        payload = item.get("payload") or {}

        saved.append({
            "id": item["id"],
            "score": item["score"],
            "role": payload.get("role"),
            "content": payload.get("content", ""),
            "timestamp": payload.get("timestamp"),
        })

    return saved


async def _save_answer(
    user_id: str,
    conversation_id: str,
    answer: str,
    results: list[dict],
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
    )
    _spawn(_index(user_id, conversation_id, "assistant", answer, message_id))

    return message_id


# ---------- The turn ----------

async def run_turn(user_id: str, conversation_id: str, query: str):
    """Yield Context, then Token (many), then Done. Raise on failure."""

    # 1. The chat must exist and belong to this user
    allowed = await asyncio.to_thread(
        repository.get_or_create_conversation,
        user_id,
        conversation_id,
        query,
    )

    if not allowed:
        raise ConversationNotFound(conversation_id)

    # 2. What was said earlier in THIS chat (read before the new question
    #    is saved, so the question is not sent twice)
    history = await asyncio.to_thread(
        repository.load_chat_history, conversation_id
    )

    # 3. Related older messages. Ask for extra: the ones already in the
    #    history are removed.
    found = await _retrieve(
        query,
        user_id,
        conversation_id,
        RETRIEVAL_LIMIT + len(history),
    )
    results = drop_messages_already_in_history(found, history)[:RETRIEVAL_LIMIT]

    yield Context(results)

    # 4. Save the question, then index it in the background (after
    #    retrieval, so the question cannot retrieve itself)
    user_message_id = await asyncio.to_thread(
        repository.add_message, conversation_id, "user", query
    )
    _spawn(_index(user_id, conversation_id, "user", query, user_message_id))

    # 5. Stream the answer
    parts: list[str] = []
    failed = False
    saving: asyncio.Future | None = None
    tokens = stream_response(query, results, history)

    try:
        async for text in tokens:
            parts.append(text)
            yield Token(text)

    except Exception:
        # The model or the network failed: nothing half-written is saved
        failed = True
        raise

    finally:
        await tokens.aclose()

        # Runs on normal completion AND when the caller cancelled (stop
        # button, closed browser). Shielded, so a second cancel cannot
        # interrupt the save.
        if not failed:
            saving = asyncio.ensure_future(
                _save_answer(user_id, conversation_id, "".join(parts), results)
            )
            await asyncio.shield(saving)

    yield Done(
        user_message_id=user_message_id,
        assistant_message_id=saving.result() if saving else "",
        answer="".join(parts),
    )
