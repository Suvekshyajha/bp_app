import json
import logging

import pytest

import jobs
import limits
import parsers
import qdrant_docs
import repository
from analysis_schema import default_analysis
from chunk_helpers import words
from fakes import FakeQdrant

SECRET = "TOP-SECRET-DOCUMENT-WORDS"


@pytest.fixture(autouse=True)
def setup(session_factory, monkeypatch):
    store = FakeQdrant()
    monkeypatch.setattr(qdrant_docs, "client", store)
    monkeypatch.setattr(limits, "ANALYSIS_ENABLED", False)
    # parse in this process: the child-process path is tested in test_parsers.py
    monkeypatch.setattr(jobs, "parse_in_child", parsers.parse_file)
    monkeypatch.setattr(
        jobs, "_embed",
        lambda texts: [{"dense": [0.1] * 4, "sparse": {"indices": [1], "values": [1.0]}} for _ in texts],
    )
    limits.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return store


@pytest.fixture
def store(setup):
    return setup


def long_text():
    return "\n\n".join(words(120, f"p{i}_") for i in range(10))


def submit(content=None, user="u1", name="a.txt", kind="txt", file_hash=None):
    content = long_text() if content is None else content
    data = content if isinstance(content, bytes) else content.encode()
    document, _ = repository.create_document(user, file_hash or ("a" * 64), name, kind)
    (limits.UPLOAD_DIR / document["id"]).write_bytes(data)
    claimed = repository.claim_next_job()
    assert claimed["id"] == document["id"]
    return document["id"]


def row(document_id):
    return repository.get_document_by_id(document_id)


# ---------- the good path ----------

def test_a_document_goes_from_pending_to_ready(store):
    document_id = submit()

    jobs.process_document(document_id)

    done = row(document_id)
    assert done["status"] == "ready"
    assert done["chunk_count"] == len(store.points) > 1
    assert done["stage"] is None and done["error"] is None
    assert {p["payload"]["status"] for p in store.points.values()} == {"ready"}
    assert {p["payload"]["document_id"] for p in store.points.values()} == {document_id}
    assert not (limits.UPLOAD_DIR / document_id).exists()  # the upload is removed


def test_the_analysis_and_report_are_saved(store):
    document_id = submit()

    jobs.process_document(document_id)

    done = row(document_id)
    analysis = json.loads(done["analysis"])
    report = json.loads(done["report"])
    assert analysis["strategy"] == "recursive" and analysis["fallback_reason"] == "analysis_disabled"
    assert report["used_strategy"] == "recursive"
    assert report["chunk_count"] == done["chunk_count"]
    assert set(report["timings"]) == {"parse_ms", "analyze_ms", "chunk_ms", "embed_ms", "store_ms", "total_ms"}


def test_chunks_become_searchable_only_at_the_very_end(store):
    document_id = submit()

    jobs.process_document(document_id)

    assert store.calls[-1] == "set_payload"
    assert store.calls.count("set_payload") == 1
    assert store.calls[0] == "delete"  # a clean start, then the upserts


def test_a_short_document_is_one_chunk_and_never_calls_the_llm(store, monkeypatch):
    monkeypatch.setattr(limits, "ANALYSIS_ENABLED", True)
    document_id = submit("A short note with only a few words in it.")

    jobs.process_document(document_id)

    assert row(document_id)["chunk_count"] == 1
    assert json.loads(row(document_id)["analysis"])["source"] == "single"


def test_embedding_runs_in_batches(store, monkeypatch):
    monkeypatch.setattr(limits, "EMBED_BATCH_SIZE", 2)
    sizes = []
    monkeypatch.setattr(
        jobs, "_embed",
        lambda texts: sizes.append(len(texts)) or [{"dense": [0.1], "sparse": {"indices": [1], "values": [1.0]}} for _ in texts],
    )
    document_id = submit()

    jobs.process_document(document_id)

    assert max(sizes) == 2 and len(sizes) > 1


def test_hidden_characters_are_stripped_before_anything_else(store):
    document_id = submit("Hello\u202e world " + words(400))

    jobs.process_document(document_id)

    texts = []
    from common.crypto import decrypt
    for point in store.points.values():
        texts.append(decrypt(point["payload"]["content"]))
    assert not any("\u202e" in text for text in texts)


def test_a_cached_analysis_is_handed_to_the_analyzer(store, monkeypatch):
    seen = {}

    def spy(parsed, stats, cached=None, client=None):
        seen["cached"] = cached
        return default_analysis(stats)

    monkeypatch.setattr(jobs, "analyze", spy)
    document_id = submit()
    repository.save_analysis(document_id, json.dumps({"source": "llm", "strategy": "recursive"}))

    jobs.process_document(document_id)

    assert seen["cached"] == {"source": "llm", "strategy": "recursive"}


def test_chunks_left_by_an_earlier_try_are_removed_first(store):
    document_id = submit()
    store.points["stale"] = {"vector": {}, "payload": {"user_id": "u1", "document_id": document_id, "status": "processing"}}

    jobs.process_document(document_id)

    assert "stale" not in store.points


def test_a_job_that_is_not_processing_is_ignored(store):
    document, _ = repository.create_document("u1", "b" * 64, "a.txt", "txt")  # still pending

    jobs.process_document(document["id"])

    assert row(document["id"])["status"] == "pending"
    assert store.points == {}


# ---------- failures ----------

def test_a_corrupt_file_fails_with_a_safe_code(store):
    document_id = submit(b"%PDF-1.4 not really", name="a.pdf", kind="pdf")

    jobs.process_document(document_id)

    failed = row(document_id)
    assert failed["status"] == "failed"
    assert failed["error"] == "unsupported_or_corrupt"
    assert store.points == {}
    assert not (limits.UPLOAD_DIR / document_id).exists()


def test_a_file_with_no_text_fails_with_no_text(store):
    document_id = submit("\u200b\u200b\u200b")

    jobs.process_document(document_id)

    assert row(document_id)["error"] == "no_text"


def test_too_many_chunks_fails_without_storing_anything(store, monkeypatch):
    monkeypatch.setattr(limits, "MAX_CHUNKS_PER_DOCUMENT", 1)
    document_id = submit()

    jobs.process_document(document_id)

    assert row(document_id)["error"] == "too_many_chunks"
    assert store.points == {}


def test_an_unexpected_error_is_a_safe_code_and_leaks_no_text(store, monkeypatch, caplog):
    monkeypatch.setattr(limits, "EMBED_BATCH_SIZE", 1)
    calls = []

    def exploding(texts):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError(f"boom {SECRET}")
        return [{"dense": [0.1], "sparse": {"indices": [1], "values": [1.0]}} for _ in texts]

    monkeypatch.setattr(jobs, "_embed", exploding)
    document_id = submit(long_text() + " " + SECRET)

    with caplog.at_level(logging.DEBUG):
        jobs.process_document(document_id)

    failed = row(document_id)
    assert failed["status"] == "failed"
    assert failed["error"] == "unsupported_or_corrupt"
    assert store.points == {}  # the first stored chunk was removed again
    assert SECRET not in caplog.text
    assert SECRET not in json.dumps({k: str(v) for k, v in failed.items()})


# ---------- deleting while a job runs ----------

def test_a_delete_in_the_middle_stops_the_job_and_removes_the_chunks(store, monkeypatch):
    monkeypatch.setattr(limits, "EMBED_BATCH_SIZE", 1)
    document_id = submit()
    calls = []

    def embed_then_delete(texts):
        calls.append(1)
        if len(calls) == 2:
            repository.mark_deleting("u1", document_id)
        return [{"dense": [0.1], "sparse": {"indices": [1], "values": [1.0]}} for _ in texts]

    monkeypatch.setattr(jobs, "_embed", embed_then_delete)

    jobs.process_document(document_id)

    assert row(document_id)["status"] == "deleting"  # never overwritten
    assert store.points == {}
    assert len(calls) == 2  # it stopped, it did not embed everything


def test_a_delete_at_the_very_last_moment_is_not_overwritten(store, monkeypatch):
    document_id = submit()
    real_mark_ready = qdrant_docs.mark_ready

    def delete_then_mark(user_id, doc_id):
        repository.mark_deleting("u1", document_id)
        real_mark_ready(user_id, doc_id)

    monkeypatch.setattr(qdrant_docs, "mark_ready", delete_then_mark)

    jobs.process_document(document_id)

    assert row(document_id)["status"] == "deleting"
    assert store.points == {}


def test_the_row_deleted_in_the_middle_also_stops_the_job(store, monkeypatch):
    monkeypatch.setattr(limits, "EMBED_BATCH_SIZE", 1)
    document_id = submit()

    def embed_then_remove_row(texts):
        repository.delete_row(document_id)
        return [{"dense": [0.1], "sparse": {"indices": [1], "values": [1.0]}} for _ in texts]

    monkeypatch.setattr(jobs, "_embed", embed_then_remove_row)

    jobs.process_document(document_id)

    assert row(document_id) is None
    assert store.points == {}
