"""ingest_client against a fake ingestion service (a real gRPC server)."""

import importlib.util
import io
from pathlib import Path

import grpc
import pytest
import pytest_asyncio

import ingest_pb2
import ingest_pb2_grpc

# Other test files put a fake "ingest_client" into sys.modules. Load the real
# file under a private name so this test always checks the real code.
_spec = importlib.util.spec_from_file_location(
    "ingest_client_under_test",
    Path(__file__).resolve().parent.parent / "ingest_client.py",
)
real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real)

TOKEN = "s3cret-token"
DOC = "11111111-1111-4111-8111-111111111111"


class FakeService(ingest_pb2_grpc.DocumentServiceServicer):
    """Records every call. Tests change `error` and `upload_answer`."""

    def __init__(self):
        self.calls = []  # (method, metadata, request or list of upload messages)
        self.error = None  # (status code, detail)
        self.upload_answer = ingest_pb2.UploadResponse(
            document=ingest_pb2.Document(id=DOC, filename="notes.txt", status="pending"),
            queued=True,
        )

    async def _record(self, method, request, context):
        self.calls.append((method, dict(context.invocation_metadata()), request))
        if self.error:
            await context.abort(*self.error)

    async def Upload(self, requests, context):
        messages = [message async for message in requests]
        await self._record("Upload", messages, context)
        return self.upload_answer

    async def ListDocuments(self, request, context):
        await self._record("ListDocuments", request, context)
        return ingest_pb2.ListDocumentsResponse(
            documents=[ingest_pb2.Document(id=DOC, filename="a.txt", status="ready", chunk_count=3)]
        )

    async def GetDocument(self, request, context):
        await self._record("GetDocument", request, context)
        return ingest_pb2.Document(id=request.document_id)

    async def DeleteDocument(self, request, context):
        await self._record("DeleteDocument", request, context)
        return ingest_pb2.DeleteDocumentResponse()


@pytest_asyncio.fixture
async def service(monkeypatch):
    fake = FakeService()
    server = grpc.aio.server()
    ingest_pb2_grpc.add_DocumentServiceServicer_to_server(fake, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    monkeypatch.setattr(real, "INGEST_SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(real, "INGEST_SERVICE_ADDRESS", f"127.0.0.1:{port}")
    monkeypatch.setattr(real, "INGEST_TLS_CA", None)
    monkeypatch.setattr(real, "USE_TLS", False)

    yield fake

    await real.close()
    await server.stop(grace=None)


@pytest.mark.asyncio
async def test_every_call_carries_the_token_and_the_user_id(service):
    await real.list_documents("alice-id")

    method, metadata, _ = service.calls[0]
    assert method == "ListDocuments"
    assert metadata["authorization"] == f"Bearer {TOKEN}"
    assert metadata["x-user-id"] == "alice-id"


@pytest.mark.asyncio
async def test_a_document_keeps_the_shape_the_browser_knows(service):
    listed = await real.list_documents("alice-id")

    assert listed == [{
        "id": DOC, "filename": "a.txt", "status": "ready", "stage": None,
        "error": None, "chunk_count": 3, "created_at": None, "chunking": None,
    }]


@pytest.mark.asyncio
async def test_upload_streams_the_name_then_the_file_and_returns_status_and_body(service, monkeypatch):
    monkeypatch.setattr(real, "UPLOAD_BLOCK", 4)

    status, body = await real.upload("alice-id", "notes.txt", io.BytesIO(b"hello world"))

    messages = service.calls[0][2]
    assert (status, body) == (202, {"id": DOC, "filename": "notes.txt", "status": "pending"})
    assert messages[0].info.filename == "notes.txt"
    assert b"".join(message.chunk for message in messages[1:]) == b"hello world"
    assert len(messages) == 4


@pytest.mark.asyncio
async def test_an_existing_document_is_200_with_every_field(service):
    service.upload_answer = ingest_pb2.UploadResponse(
        document=ingest_pb2.Document(id=DOC, filename="notes.txt", status="ready", chunk_count=2),
        existing=True,
    )

    status, body = await real.upload("alice-id", "notes.txt", io.BytesIO(b"hello"))

    assert status == 200
    assert body["chunk_count"] == 2 and "chunking" in body


@pytest.mark.asyncio
async def test_get_and_delete_send_the_document_id(service):
    assert (await real.get_document("alice-id", DOC))["id"] == DOC
    assert await real.delete_document("alice-id", DOC) is None

    assert [(method, request.document_id) for method, _, request in service.calls] == [
        ("GetDocument", DOC),
        ("DeleteDocument", DOC),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", [
    (grpc.StatusCode.PERMISSION_DENIED, 400),
    (grpc.StatusCode.UNAUTHENTICATED, 401),
    (grpc.StatusCode.NOT_FOUND, 404),
    (grpc.StatusCode.FAILED_PRECONDITION, 409),
    (grpc.StatusCode.OUT_OF_RANGE, 413),
    (grpc.StatusCode.INVALID_ARGUMENT, 415),
])
async def test_status_codes_become_ingest_error_with_the_detail(service, code, status):
    service.error = (code, "why")

    with pytest.raises(real.IngestError) as error:
        await real.get_document("alice-id", DOC)

    assert (error.value.status, error.value.detail) == (status, "why")


@pytest.mark.asyncio
async def test_a_refused_upload_keeps_its_status(service):
    service.error = (grpc.StatusCode.OUT_OF_RANGE, "File is larger than 10 MB.")

    with pytest.raises(real.IngestError) as error:
        await real.upload("alice-id", "notes.txt", io.BytesIO(b"hello"))

    assert (error.value.status, error.value.detail) == (413, "File is larger than 10 MB.")


@pytest.mark.asyncio
async def test_an_unexpected_failure_is_500_without_details(service):
    service.error = (grpc.StatusCode.INTERNAL, "secret trace")

    with pytest.raises(real.IngestError) as error:
        await real.list_documents("alice-id")

    assert (error.value.status, error.value.detail) == (500, "")


@pytest.mark.asyncio
async def test_an_unreachable_service_is_503_without_details(service, monkeypatch):
    monkeypatch.setattr(real, "INGEST_SERVICE_ADDRESS", "127.0.0.1:1")

    with pytest.raises(real.IngestError) as error:
        await real.list_documents("alice-id")

    assert (error.value.status, error.value.detail) == (503, real.UNAVAILABLE_DETAIL)


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["", None])
async def test_a_call_without_a_user_id_is_refused_before_any_request(service, user):
    with pytest.raises(ValueError):
        await real.list_documents(user)

    assert service.calls == []


def test_an_old_url_setting_gives_the_address():
    assert real._address_from_url("http://ingestion:8100/") == "ingestion:8100"


@pytest.mark.asyncio
async def test_close_drops_the_channel(service):
    await real.list_documents("alice-id")
    await real.close()

    assert real._channel is None and real._stub is None
