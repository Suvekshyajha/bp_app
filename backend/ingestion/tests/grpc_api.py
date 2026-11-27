"""A real ingestion server on a free local port, and a small client for tests."""

import grpc

import ingest_pb2
import ingest_pb2_grpc
import main

TOKEN = "test-token"
OK = grpc.StatusCode.OK


def metadata(user="user-a", token=TOKEN):
    entries = []
    if token is not None:
        entries.append(("authorization", f"Bearer {token}"))
    if user is not None:
        entries.append(("x-user-id", user))
    return tuple(entries)


class Result:
    def __init__(self, code=OK, detail="", value=None):
        self.code = code
        self.detail = detail
        self.value = value  # the answer message when the call worked


class Api:
    def __init__(self):
        self.server, port = main.build_server("127.0.0.1:0")
        self.server.start()
        self.channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        self.stub = ingest_pb2_grpc.DocumentServiceStub(self.channel)

    def close(self):
        self.channel.close()
        self.server.stop(grace=None).wait()

    def call(self, method, request, entries) -> Result:
        try:
            value = getattr(self.stub, method)(request, metadata=entries, timeout=10)
        except grpc.RpcError as error:
            return Result(error.code(), error.details())
        return Result(value=value)

    def upload(self, content=b"Hello world, some text.", name="notes.txt", user="user-a", block=1024):
        def pieces():
            yield ingest_pb2.UploadRequest(info=ingest_pb2.UploadInfo(filename=name))
            for first in range(0, len(content), block):
                yield ingest_pb2.UploadRequest(chunk=content[first : first + block])

        return self.call("Upload", pieces(), metadata(user))

    def list(self, user="user-a"):
        return self.call("ListDocuments", ingest_pb2.ListDocumentsRequest(), metadata(user))

    def get(self, document_id, user="user-a"):
        request = ingest_pb2.GetDocumentRequest(document_id=document_id)
        return self.call("GetDocument", request, metadata(user))

    def delete(self, document_id, user="user-a"):
        request = ingest_pb2.DeleteDocumentRequest(document_id=document_id)
        return self.call("DeleteDocument", request, metadata(user))
