"""Upload through the API, run the worker once, read the result back."""

import json

import pytest
from fastapi.testclient import TestClient

import analyzer
import jobs
import limits
import qdrant_docs
import worker
from chunk_helpers import words
from fakes import FakeGroq, FakeQdrant, HttpError

HEADERS = {"Authorization": "Bearer test-token", "X-User-Id": "user-a"}


@pytest.fixture
def system(session_factory, monkeypatch):
    import main

    store = FakeQdrant()
    monkeypatch.setattr(qdrant_docs, "client", store)
    monkeypatch.setattr(
        jobs, "_embed",
        lambda texts: [{"dense": [0.1], "sparse": {"indices": [1], "values": [1.0]}} for _ in texts],
    )
    with TestClient(main.app) as client:
        yield client, store


def upload_long_document(client):
    text = "\n\n".join(words(150, f"p{i}_") for i in range(8))
    response = client.post("/documents", files={"file": ("guide.txt", text.encode(), "text/plain")}, headers=HEADERS)
    assert response.status_code == 202
    return response.json()["id"]


def test_upload_then_worker_then_ready(system, monkeypatch):
    client, store = system
    monkeypatch.setattr(limits, "ANALYSIS_ENABLED", False)
    document_id = upload_long_document(client)
    assert client.get(f"/documents/{document_id}", headers=HEADERS).json()["status"] == "pending"

    assert worker.run_once() is True  # a real child process parses the file

    shown = client.get(f"/documents/{document_id}", headers=HEADERS).json()
    assert shown["status"] == "ready"
    assert shown["chunk_count"] == len(store.points) > 1
    assert shown["chunking"]["strategy_used"] == "recursive"
    assert shown["chunking"]["fallback_reason"] == "analysis_disabled"
    assert worker.run_once() is False


def test_a_failing_llm_still_ends_ready_with_a_fallback_reason(system, monkeypatch):
    client, store = system
    monkeypatch.setattr(limits, "ANALYSIS_ENABLED", True)
    monkeypatch.setattr(analyzer, "_make_client", lambda: FakeGroq([HttpError(401, "invalid api key")]))
    document_id = upload_long_document(client)

    worker.run_once()

    shown = client.get(f"/documents/{document_id}", headers=HEADERS).json()
    assert shown["status"] == "ready"
    assert shown["chunking"]["fallback_reason"] == "llm_error"
    assert shown["chunking"]["strategy_used"] in ("recursive", "paragraph", "heading_aware", "hybrid")


def test_a_good_llm_answer_is_used_and_saved_for_retries(system, monkeypatch):
    client, store = system
    monkeypatch.setattr(limits, "ANALYSIS_ENABLED", True)
    answer = json.dumps({
        "document_structure": "unstructured",
        "recommended_strategy": "fixed_size",
        "chunking_parameters": {"chunk_size": 200, "chunk_overlap": 20, "unit": "tokens"},
    })
    monkeypatch.setattr(analyzer, "_make_client", lambda: FakeGroq([answer]))
    document_id = upload_long_document(client)

    worker.run_once()

    shown = client.get(f"/documents/{document_id}", headers=HEADERS).json()
    assert shown["chunking"]["strategy_used"] == "fixed_size"
    import repository
    assert json.loads(repository.get_document_by_id(document_id)["analysis"])["source"] == "llm"


def test_deleting_after_ready_removes_everything(system, monkeypatch):
    client, store = system
    monkeypatch.setattr(limits, "ANALYSIS_ENABLED", False)
    document_id = upload_long_document(client)
    worker.run_once()
    assert store.points

    assert client.delete(f"/documents/{document_id}", headers=HEADERS).status_code == 204

    assert store.points == {}
    assert client.get(f"/documents/{document_id}", headers=HEADERS).status_code == 404
