import asyncio
import logging
import os
import uuid
from typing import AsyncIterator, Callable

import grpc
from dotenv import load_dotenv
from google.protobuf.json_format import MessageToDict

import retriever_pb2
import retriever_pb2_grpc

load_dotenv()

logger = logging.getLogger(__name__)

RETRIEVER_ADDRESS = os.getenv("RETRIEVER_ADDRESS", "localhost:50051")

# How long to wait for the next message of an answer
RESPONSE_TIMEOUT_SECONDS = 30


class RetrieverError(Exception):
    """The retriever could not answer (it was down, or it reported an error)."""


class _Closed:
    """Put in a waiting caller's queue when the stream died."""

    def __init__(self, reason: str):
        self.reason = reason


_STOP = object()


class _Multiplexer:
    """One long-lived bidirectional stream shared by many concurrent requests.

    Requests go out on the stream as they are made. Every reply carries the
    request_id of its request, so replies are handed back to the right caller
    even when they arrive in a different order. If the stream breaks, every
    waiting caller gets an error and the next call opens a fresh stream.
    """

    def __init__(
        self,
        open_call: Callable,
        is_last: Callable[[object], bool],
        name: str,
    ):
        self._open_call = open_call
        self._is_last = is_last
        self._name = name
        self._outbox: asyncio.Queue | None = None
        self._pending: dict[str, asyncio.Queue] = {}
        self._reader: asyncio.Task | None = None

    # No `await` in here on purpose: checking the stream and registering a
    # caller must happen without the reader task getting a chance to run.
    def _ensure_started(self) -> asyncio.Queue:
        if self._reader is not None and not self._reader.done():
            return self._outbox

        outbox: asyncio.Queue = asyncio.Queue()

        async def requests():
            while True:
                item = await outbox.get()
                if item is _STOP:
                    return
                yield item

        call = self._open_call(requests())
        self._outbox = outbox
        self._reader = asyncio.create_task(self._read(call))
        logger.info("Opened %s stream", self._name)
        return outbox

    async def _read(self, call) -> None:
        reason = "the retriever closed the stream"

        try:
            async for message in call:
                queue = self._pending.get(message.request_id)

                if queue is not None:
                    queue.put_nowait(message)

        except grpc.aio.AioRpcError as error:
            reason = f"{error.code().name}: {error.details()}"
            logger.warning("%s stream failed: %s", self._name, reason)

        except asyncio.CancelledError:
            call.cancel()
            reason = "the client is shutting down"

        finally:
            for queue in list(self._pending.values()):
                queue.put_nowait(_Closed(reason))

            self._pending.clear()

    async def call(self, request) -> AsyncIterator:
        """Send one request and yield its replies until the last one."""

        outbox = self._ensure_started()

        request_id = uuid.uuid4().hex
        request.request_id = request_id

        queue: asyncio.Queue = asyncio.Queue()
        self._pending[request_id] = queue
        outbox.put_nowait(request)

        try:
            while True:
                try:
                    message = await asyncio.wait_for(
                        queue.get(),
                        RESPONSE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    raise RetrieverError("The retriever took too long to answer.")

                if isinstance(message, _Closed):
                    raise RetrieverError(f"Retriever stream ended: {message.reason}")

                yield message

                if self._is_last(message):
                    return

        finally:
            # Also runs if the caller stops early (cancelled or breaks out)
            self._pending.pop(request_id, None)

    async def close(self) -> None:
        if self._outbox is not None:
            self._outbox.put_nowait(_STOP)  # ends our side of the stream

        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)


_channel: grpc.aio.Channel | None = None
_retrieve_stream: _Multiplexer | None = None
_store_stream: _Multiplexer | None = None


def _streams() -> tuple[_Multiplexer, _Multiplexer]:
    """Create the channel and streams on first use (needs a running event loop)."""

    global _channel, _retrieve_stream, _store_stream

    if _channel is None:
        _channel = grpc.aio.insecure_channel(RETRIEVER_ADDRESS)
        stub = retriever_pb2_grpc.RetrieverServiceStub(_channel)

        _retrieve_stream = _Multiplexer(
            stub.RetrieveData,
            is_last=lambda chunk: chunk.last,
            name="RetrieveData",
        )
        _store_stream = _Multiplexer(
            stub.StoreMessage,
            is_last=lambda response: True,  # one reply per request
            name="StoreMessage",
        )

    return _retrieve_stream, _store_stream


async def retrieve_stream(
    query: str,
    user_id: str,
    limit: int = 5,
) -> AsyncIterator[dict]:
    """Yield retrieved chunks one by one, best match first, as they arrive."""

    retrieve_mux, _ = _streams()

    request = retriever_pb2.RetrieveRequest(
        query=query,
        user_id=user_id,
        limit=limit,
    )

    async for chunk in retrieve_mux.call(request):
        if chunk.error:
            raise RetrieverError(chunk.error)

        if chunk.last:
            return

        yield {
            "id": chunk.id,
            "score": chunk.score,
            "payload": MessageToDict(chunk.payload),
        }


async def retrieve(query: str, user_id: str, limit: int = 5) -> list[dict]:
    """Same as retrieve_stream, collected into a list."""

    return [
        item
        async for item in retrieve_stream(query, user_id, limit)
    ]


async def store(
    user_id: str,
    conversation_id: str,
    role: str,
    content: str,
) -> str:
    _, store_mux = _streams()

    request = retriever_pb2.StoreMessageRequest(
        user_id=user_id,
        conversation_id=conversation_id,
        role=role,
        content=content,
    )

    async for response in store_mux.call(request):
        if response.error:
            raise RetrieverError(response.error)

        return response.message_id

    raise RetrieverError("The retriever sent no reply.")


async def close() -> None:
    """Call on app shutdown."""

    global _channel, _retrieve_stream, _store_stream

    for mux in (_retrieve_stream, _store_stream):
        if mux is not None:
            await mux.close()

    if _channel is not None:
        await _channel.close()

    _channel = _retrieve_stream = _store_stream = None