import asyncio
import logging

import grpc
from google.protobuf.json_format import ParseDict

import retriever_pb2
import retriever_pb2_grpc

from retrieval import retrieve_messages
from qdrant_store import create_collection, store_message


logger = logging.getLogger(__name__)

# Embedding is CPU-heavy: never run more than this many at the same time
MAX_CONCURRENT_JOBS = 4
_jobs = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

_DONE = object()


async def serve_stream(request_iterator, handle):
    """Run a bidirectional stream.

    Reads requests as they arrive and handles each one in its own task, so a
    slow request does not hold up the ones behind it. Whatever `handle(request)`
    yields is sent back right away. Ends when the client closes its side and
    every request has been answered.
    """
    outbox: asyncio.Queue = asyncio.Queue()
    in_flight: set[asyncio.Task] = set()

    async def run_one(request):
        async for response in handle(request):
            await outbox.put(response)

    async def read_requests():
        try:
            async for request in request_iterator:
                task = asyncio.create_task(run_one(request))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)

            # Client finished sending: wait for the answers still being made
            await asyncio.gather(*list(in_flight))
        finally:
            await outbox.put(_DONE)

    reader = asyncio.create_task(read_requests())

    try:
        while True:
            item = await outbox.get()
            if item is _DONE:
                break
            yield item
    finally:
        # Client went away (or we are done): stop everything still running
        reader.cancel()
        for task in list(in_flight):
            task.cancel()
        await asyncio.gather(reader, *list(in_flight), return_exceptions=True)


class RetrieverService(retriever_pb2_grpc.RetrieverServiceServicer):

    async def RetrieveData(self, request_iterator, context):
        async for response in serve_stream(request_iterator, self._retrieve_one):
            yield response

    async def StoreMessage(self, request_iterator, context):
        async for response in serve_stream(request_iterator, self._store_one):
            yield response

    # One request in, several chunks out. A bad request gets an error on its
    # final message; it does not close the stream for everyone else.
    async def _retrieve_one(self, request):
        request_id = request.request_id

        if (
            not request.user_id
            or not request.conversation_id
            or not request.query.strip()
        ):
            yield retriever_pb2.RetrievedChunk(
                request_id=request_id,
                last=True,
                error="user_id, conversation_id and query are required",
            )
            return

        try:
            async with _jobs:
                results = await asyncio.to_thread(
                    retrieve_messages,
                    query=request.query,
                    user_id=request.user_id,
                    conversation_id=request.conversation_id,
                    limit=request.limit or 5,
                )

            # Best match first, each chunk sent as soon as it is built
            for result in results:
                chunk = retriever_pb2.RetrievedChunk(
                    id=result["id"],
                    score=result["score"],
                    request_id=request_id,
                )

                if result["payload"]:
                    ParseDict(result["payload"], chunk.payload)

                yield chunk

            yield retriever_pb2.RetrievedChunk(request_id=request_id, last=True)

        except Exception as error:
            logger.exception("RetrieveData failed")
            yield retriever_pb2.RetrievedChunk(
                request_id=request_id,
                last=True,
                error=str(error),
            )

    async def _store_one(self, request):
        request_id = request.request_id

        if (
            not request.user_id
            or not request.conversation_id
            or not request.content.strip()
        ):
            yield retriever_pb2.StoreMessageResponse(
                request_id=request_id,
                error="user_id, conversation_id and content are required",
            )
            return

        try:
            async with _jobs:
                message_id = await asyncio.to_thread(
                    store_message,
                    user_id=request.user_id,
                    conversation_id=request.conversation_id,
                    role=request.role,
                    content=request.content,
                )

            yield retriever_pb2.StoreMessageResponse(
                request_id=request_id,
                message_id=message_id,
            )

        except Exception as error:
            logger.exception("StoreMessage failed")
            yield retriever_pb2.StoreMessageResponse(
                request_id=request_id,
                error=str(error),
            )


async def serve():
    # Create Qdrant collection if it doesn't exist
    await asyncio.to_thread(create_collection)

    server = grpc.aio.server()

    retriever_pb2_grpc.add_RetrieverServiceServicer_to_server(
        RetrieverService(),
        server,
    )

    server.add_insecure_port("[::]:50051")

    await server.start()

    print("Retriever gRPC server (bidirectional streaming) running on port 50051")

    try:
        await server.wait_for_termination()
    except asyncio.CancelledError:
        await server.stop(grace=5)
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())