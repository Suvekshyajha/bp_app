import grpc
from google.protobuf.json_format import MessageToDict

import retriever_pb2
import retriever_pb2_grpc

import os
from dotenv import load_dotenv

load_dotenv()  

RETRIEVER_ADDRESS = os.getenv("RETRIEVER_ADDRESS", "localhost:50051")

channel = grpc.insecure_channel(RETRIEVER_ADDRESS)
stub = retriever_pb2_grpc.RetrieverServiceStub(channel)




def retrieve(
    query: str,
    user_id: str,
    limit: int = 5,
) -> list[dict]:

    with grpc.insecure_channel(RETRIEVER_ADDRESS) as channel:
        stub = retriever_pb2_grpc.RetrieverServiceStub(channel)

        request = retriever_pb2.RetrieveRequest(
            query=query,
            user_id=user_id,
            limit=limit,
        )

        # The server now streams the chunks back one by one
        stream = stub.RetrieveData(request, timeout=30)

        # Read the whole stream here, inside the `with`. If we returned the
        # stream itself, the channel would close before we read from it.
        return [
            {
                "id": chunk.id,
                "score": chunk.score,
                "payload": MessageToDict(chunk.payload),
            }
            for chunk in stream
        ]


def store(
    user_id: str,
    conversation_id: str,
    role: str,
    content: str,
) -> str:

    with grpc.insecure_channel(RETRIEVER_ADDRESS) as channel:
        stub = retriever_pb2_grpc.RetrieverServiceStub(channel)

        request = retriever_pb2.StoreMessageRequest(
            user_id=user_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
        )

        response = stub.StoreMessage(request, timeout=30)

        return response.message_id