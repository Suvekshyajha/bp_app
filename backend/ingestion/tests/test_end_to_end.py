"""Upload through the service, run the worker once, read the result back."""

import json

import grpc
import pytest

import analyzer
import jobs
import limits
import qdrant_docs
import worker
from chunk_helpers import words
from fakes import FakeGroq, FakeQdrant, HttpError
from grpc_api import OK, Api


@pytest.fixture
def system(session_factory, monkeypatch):
    store = FakeQdrant()
    monkeypatch.setattr(qdrant_docs, "client", store)
    monkeypatch.setattr(
        jobs, "_embed",
        lambda texts: [{"dense": [0.1], "sparse": {"indices": [1], "values": [1.0]}} for _ in texts],
    )
    api = Api()
    yield api, store
    api.close()


def upload_long_document(api):
    text = "\n\n".join(words(150, f"p{i}_") for i in range(8))
    result = api.upload(content=text.encode(), name="guide.txt")
    assert result.code == OK and result.value.queued
    return result.value.document.id


def test_upload_then_worker_then_ready(system, monkeypatch):
    api, store = system
    monkeypatch.setattr(limits, "ANALYSIS_ENABLED", False)
    document_id = upload_long_document(api)
    assert api.get(document_id).value.status == "pending"

    assert worker.run_once() is True  # a real child process parses the file

    shown = api.get(document_id).value
    assert shown.status == "ready"
    assert shown.chunk_count == len(store.points) > 1
    assert shown.chunking.strategy_used == "recursive"
    assert shown.chunking.fallback_reason == "analysis_disabled"
    assert worker.run_once() is False


def test_a_failing_llm_still_ends_ready_with_a_fallback_reason(system, monkeypatch):
    api, store = system
    monkeypatch.setattr(limits, "ANALYSIS_ENABLED", True)
    monkeypatch.setattr(analyzer, "_make_client", lambda: FakeGroq([HttpError(401, "invalid api key")]))
    document_id = upload_long_document(api)

    worker.run_once()

    shown = api.get(document_id).value
    assert shown.status == "ready"
    assert shown.chunking.fallback_reason == "llm_error"
    assert shown.chunking.strategy_used in ("recursive", "paragraph", "heading_aware", "hybrid")


def test_a_good_llm_answer_is_used_and_saved_for_retries(system, monkeypatch):
    api, store = system
    monkeypatch.setattr(limits, "ANALYSIS_ENABLED", True)
    answer = json.dumps({
        "document_structure": "unstructured",
        "recommended_strategy": "fixed_size",
        "chunking_parameters": {"chunk_size": 200, "chunk_overlap": 20, "unit": "tokens"},
    })
    monkeypatch.setattr(analyzer, "_make_client", lambda: FakeGroq([answer]))
    document_id = upload_long_document(api)

    worker.run_once()

    shown = api.get(document_id).value
    assert shown.chunking.strategy_used == "fixed_size"
    import repository
    assert json.loads(repository.get_document_by_id(document_id)["analysis"])["source"] == "llm"


def test_deleting_after_ready_removes_everything(system, monkeypatch):
    api, store = system
    monkeypatch.setattr(limits, "ANALYSIS_ENABLED", False)
    document_id = upload_long_document(api)
    worker.run_once()
    assert store.points

    assert api.delete(document_id).code == OK

    assert store.points == {}
    assert api.get(document_id).code == grpc.StatusCode.NOT_FOUND
