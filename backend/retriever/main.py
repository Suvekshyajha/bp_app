from concurrent import futures

import grpc
from google.protobuf.json_format import ParseDict

import retriever_pb2
import retriever_pb2_grpc

from retrieval import retrieve_messages
from qdrant_store import create_collection, store_message


class RetrieverService(retriever_pb2_grpc.RetrieverServiceServicer):

    def RetrieveData(self, request, context):
        if not request.user_id or not request.query.strip():
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "user_id and query are required",
            )

        try:
            results = retrieve_messages(
                query=request.query,
                user_id=request.user_id,
                limit=request.limit or 5,
            )

            response = retriever_pb2.RetrieveResponse()

            for result in results:
                message = response.results.add()
                message.id = result["id"]
                message.score = result["score"]

                if result["payload"]:
                    ParseDict(
                        result["payload"],
                        message.payload,
                    )

            return response

        except Exception as error:
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(error))
            return retriever_pb2.RetrieveResponse()

    def StoreMessage(self, request, context):
        if (
            not request.user_id
            or not request.conversation_id
            or not request.content.strip()
        ):
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "user_id, conversation_id and content are required",
            )

        try:
            message_id = store_message(
                user_id=request.user_id,
                conversation_id=request.conversation_id,
                role=request.role,
                content=request.content,
            )

            return retriever_pb2.StoreMessageResponse(
                message_id=message_id
            )

        except Exception as error:
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(error))
            return retriever_pb2.StoreMessageResponse()


def serve():
    # Create Qdrant collection if it doesn't exist
    create_collection()

    # Initialize gRPC server
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=10)
    )

    retriever_pb2_grpc.add_RetrieverServiceServicer_to_server(
        RetrieverService(),
        server,
    )

    server.add_insecure_port("[::]:50051")

    server.start()

    print("Retriever gRPC server running on port 50051")

    server.wait_for_termination()


if __name__ == "__main__":
    serve()