"""ingest_client against a fake HTTP service (httpx.MockTransport)."""

import importlib.util
import io
from pathlib import Path

import httpx
import pytest

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


@pytest.fixture
def service(monkeypatch):
    """Install a real client wired to `service.handler`. Records every request."""

    monkeypatch.setattr(real, "INGEST_SERVICE_TOKEN", TOKEN)
    monkeypatch.setattr(real, "INGEST_SERVICE_URL", "http://ingest.test")

    class Service:
        def __init__(self):
            self.requests = []
            self.handler = lambda request: httpx.Response(200, json=[])

        def __call__(self, request):
            self.requests.append(request)
            return self.handler(request)

    fake = Service()
    monkeypatch.setattr(real, "_client", real._make_client(transport=httpx.MockTransport(fake)))
    return fake


@pytest.mark.asyncio
async def test_every_call_carries_the_token_and_the_user_id(service):
    await real.list_documents("alice-id")

    request = service.requests[0]
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["x-user-id"] == "alice-id"
    assert (request.method, request.url.path) == ("GET", "/documents")


@pytest.mark.asyncio
async def test_upload_sends_the_file_as_multipart_and_returns_status_and_body(service):
    service.handler = lambda request: httpx.Response(202, json={"id": DOC, "status": "pending"})

    status, body = await real.upload("alice-id", "notes.txt", io.BytesIO(b"hello world"))

    request = service.requests[0]
    assert (status, body) == (202, {"id": DOC, "status": "pending"})
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b'filename="notes.txt"' in request.content
    assert b"hello world" in request.content
    assert request.headers["x-user-id"] == "alice-id"


@pytest.mark.asyncio
async def test_get_and_delete_use_the_document_path(service):
    service.handler = lambda request: (
        httpx.Response(204) if request.method == "DELETE" else httpx.Response(200, json={"id": DOC})
    )

    assert await real.get_document("alice-id", DOC) == {"id": DOC}
    assert await real.delete_document("alice-id", DOC) is None
    assert [(r.method, r.url.path) for r in service.requests] == [
        ("GET", f"/documents/{DOC}"),
        ("DELETE", f"/documents/{DOC}"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 404, 409, 413, 415, 500])
async def test_error_statuses_become_ingest_error_with_the_detail(service, status):
    service.handler = lambda request: httpx.Response(status, json={"detail": "why"})

    with pytest.raises(real.IngestError) as error:
        await real.get_document("alice-id", DOC)

    assert (error.value.status, error.value.detail) == (status, "why")


@pytest.mark.asyncio
async def test_an_error_without_json_has_an_empty_detail(service):
    service.handler = lambda request: httpx.Response(502, text="<html>bad gateway</html>")

    with pytest.raises(real.IngestError) as error:
        await real.list_documents("alice-id")

    assert (error.value.status, error.value.detail) == (502, "")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    httpx.ConnectError("refused"),
    httpx.ReadTimeout("slow"),
    httpx.ConnectTimeout("slow"),
])
async def test_an_unreachable_service_is_503_without_details(service, failure):
    def refuse(request):
        raise failure

    service.handler = refuse

    with pytest.raises(real.IngestError) as error:
        await real.list_documents("alice-id")

    assert error.value.status == 503
    assert "refused" not in error.value.detail


@pytest.mark.asyncio
async def test_an_unreadable_success_answer_is_502(service):
    service.handler = lambda request: httpx.Response(200, text="not json")

    with pytest.raises(real.IngestError) as error:
        await real.list_documents("alice-id")

    assert error.value.status == 502


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["", None])
async def test_a_call_without_a_user_id_is_refused_before_any_request(service, user):
    with pytest.raises(ValueError):
        await real.list_documents(user)

    assert service.requests == []


@pytest.mark.asyncio
async def test_close_drops_the_client(service):
    await real.close()

    assert real._client is None
