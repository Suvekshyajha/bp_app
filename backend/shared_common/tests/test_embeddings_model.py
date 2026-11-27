"""What is sent to the Gemini API and what is made of the answer. No real call:
the HTTP client is replaced, so no key or network is needed."""

import sys
import types

import pytest

from common import embeddings, settings


class _Vector(list):
    def tolist(self):
        return list(self)


def _sparse_vector():
    return types.SimpleNamespace(indices=_Vector([1]), values=_Vector([1.0]))


class FakeSparse:
    def embed(self, texts):
        for _ in texts:
            yield _sparse_vector()

    def query_embed(self, text):
        yield _sparse_vector()


class FakeClient:
    """Answers every text with the vector (3, 4, 0, 0, ...)."""

    def __init__(self, size=settings.DENSE_DIM):
        self.size = size
        self.requests = []

    def post(self, path, json, timeout):
        self.requests.append((path, json))
        values = [3.0, 4.0] + [0.0] * (self.size - 2)
        answer = {"embeddings": [{"values": values} for _ in json["requests"]]}

        return types.SimpleNamespace(status_code=200, json=lambda: answer)

    def sent_texts(self):
        return [
            item["content"]["parts"][0]["text"]
            for _, body in self.requests
            for item in body["requests"]
        ]


@pytest.fixture
def client(monkeypatch):
    # The module imports httpx when it sends a request. The client is fake, so
    # the library does not have to be installed to run these tests.
    fake_httpx = types.ModuleType("httpx")
    fake_httpx.HTTPError = type("HTTPError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)

    fake = FakeClient()
    monkeypatch.setattr(embeddings, "_dense", fake)
    monkeypatch.setattr(embeddings, "_sparse", FakeSparse())

    return fake


def test_the_dense_model_is_gemini_with_768_numbers():
    assert settings.DENSE_MODEL == "gemini-embedding-2"
    assert settings.DENSE_DIM == 768


def test_a_query_is_sent_with_its_task_and_the_vector_size(client):
    embeddings.embed_query("what is my dog called?")

    path, body = client.requests[0]
    assert path == f"/models/{settings.DENSE_MODEL}:batchEmbedContents"
    assert body["requests"][0]["outputDimensionality"] == settings.DENSE_DIM
    assert client.sent_texts() == ["task: search result | query: what is my dog called?"]


def test_a_passage_is_sent_in_the_document_format(client):
    embeddings.embed_passages(["my dog is called Bo"])

    assert client.sent_texts() == ["title: none | text: my dog is called Bo"]


def test_a_very_long_text_is_cut_before_it_is_sent(client):
    embeddings.embed_query("a" * (settings.DENSE_MAX_CHARS + 100))

    sent = client.sent_texts()[0]
    assert sent == embeddings.QUERY_FORMAT.format("a" * settings.DENSE_MAX_CHARS)


def test_texts_are_sent_in_batches(client):
    results = embeddings.embed_passages(["text"] * (embeddings.BATCH_SIZE + 1))

    sizes = [len(body["requests"]) for _, body in client.requests]
    assert sizes == [embeddings.BATCH_SIZE, 1]
    assert len(results) == embeddings.BATCH_SIZE + 1


def test_a_dense_vector_is_made_unit_length(client):
    dense = embeddings.embed_query("hello")["dense"]

    assert dense[:2] == pytest.approx([0.6, 0.8])


def test_a_vector_of_the_wrong_size_is_refused(client):
    client.size = 10

    with pytest.raises(embeddings.EmbeddingError):
        embeddings.embed_query("hello")


@pytest.mark.parametrize("value", ["", "your_key_here"])
def test_a_missing_key_is_refused(monkeypatch, value):
    monkeypatch.setenv("GEMINI_API_KEY", value)

    with pytest.raises(embeddings.EmbeddingError):
        embeddings.require_api_key()
