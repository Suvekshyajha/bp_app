"""chat_client: document_ids reach the chat service and excerpts keep their source."""

import importlib.util
from pathlib import Path

import pytest

import chat_pb2

# Load the real chat_client under a private name (other tests install a fake).
_spec = importlib.util.spec_from_file_location(
    "chat_client_docs_under_test",
    Path(__file__).resolve().parent.parent / "chat_client.py",
)
real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real)

DOC = "11111111-1111-4111-8111-111111111111"


class FakeCall:
    def __init__(self, events):
        self._events = events
        self.cancelled = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for event in self._events:
            yield event

    def cancel(self):
        self.cancelled = True


class FakeChatStub:
    def __init__(self, events):
        self.requests = []
        self._events = events

    def Chat(self, request, timeout=None, metadata=None):
        self.requests.append(request)
        return FakeCall(self._events)


def excerpt(**fields):
    base = dict(
        id="c1",
        similarity=0.8,
        role=chat_pb2.ROLE_UNSPECIFIED,
        content="Revenue was five.",
        document_id=DOC,
        filename="report.pdf",
        chunk_index=2,
        section_title="Results",
        page=3,
    )
    base.update(fields)
    return chat_pb2.RetrievedMessage(**base)


def past_message():
    return chat_pb2.RetrievedMessage(
        id="m1", similarity=0.7, role=chat_pb2.ROLE_USER, content="earlier"
    )


@pytest.fixture
def stub(monkeypatch):
    events = [
        chat_pb2.ChatEvent(context=chat_pb2.Context(messages=[past_message(), excerpt()])),
        chat_pb2.ChatEvent(token=chat_pb2.Token(text="Hi")),
        chat_pb2.ChatEvent(done=chat_pb2.Done(answer="Hi", user_message_id="u", assistant_message_id="a")),
    ]
    fake = FakeChatStub(events)
    monkeypatch.setattr(real, "_stubs", lambda: (fake, None))
    return fake


async def collect(**kwargs):
    return [item async for item in real.chat("alice-id", "chat-0001", "hello", **kwargs)]


@pytest.mark.asyncio
async def test_document_ids_are_sent_in_the_request(stub):
    await collect(document_ids=[DOC])

    assert list(stub.requests[0].document_ids) == [DOC]


@pytest.mark.asyncio
async def test_no_document_ids_by_default(stub):
    await collect()

    assert list(stub.requests[0].document_ids) == []


@pytest.mark.asyncio
async def test_an_excerpt_carries_its_source_in_the_payload(stub):
    events = await collect(document_ids=[DOC])
    _, results = events[0]
    payload = results[1]["payload"]

    assert payload["document_id"] == DOC
    assert payload["filename"] == "report.pdf"
    assert payload["chunk_index"] == 2
    assert payload["section_title"] == "Results"
    assert payload["page"] == 3
    assert payload["content"] == "Revenue was five."


@pytest.mark.asyncio
async def test_a_past_chat_message_keeps_exactly_its_old_shape(stub):
    events = await collect(document_ids=[DOC])
    _, results = events[0]

    assert set(results[0]["payload"]) == {"message_id", "role", "content", "timestamp"}


def test_empty_section_and_page_are_left_out_but_part_zero_stays():
    payload = real._result_for_frontend(excerpt(section_title="", page=0, chunk_index=0))["payload"]

    assert payload["chunk_index"] == 0
    assert "section_title" not in payload
    assert "page" not in payload
