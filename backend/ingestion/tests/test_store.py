import pytest

import qdrant_docs
from common.crypto import decrypt
from doc_model import Chunk
from fakes import FakeQdrant


@pytest.fixture
def fake(monkeypatch):
    store = FakeQdrant()
    monkeypatch.setattr(qdrant_docs, "client", store)
    return store


def chunks(count=3):
    return [Chunk(i, f"text of chunk {i}", 4, f"Title {i}" if i else None, 2, [(0, 0, 5)]) for i in range(count)]


def vectors(count=3):
    return [{"dense": [0.1] * 768, "sparse": {"indices": [1, 2], "values": [0.5, 0.5]}} for _ in range(count)]


def store(user="u1", doc="d1", file_hash="h" * 64, **kw):
    qdrant_docs.upsert_chunks(user, doc, file_hash, "report.pdf", "recursive", kw.get("chunks", chunks()), kw.get("vectors", vectors()))


def test_chunk_ids_are_stable_and_depend_on_user_file_and_number():
    first = qdrant_docs.chunk_point_id("u1", "h", 0)

    assert first == qdrant_docs.chunk_point_id("u1", "h", 0)
    assert first != qdrant_docs.chunk_point_id("u1", "h", 1)
    assert first != qdrant_docs.chunk_point_id("u2", "h", 0)
    assert first != qdrant_docs.chunk_point_id("u1", "other", 0)


def test_chunks_are_stored_processing_with_encrypted_text(fake):
    store()

    assert len(fake.points) == 3
    payload = fake.points[qdrant_docs.chunk_point_id("u1", "h" * 64, 1)]["payload"]
    assert payload["status"] == "processing"
    assert payload["user_id"] == "u1" and payload["document_id"] == "d1"
    assert payload["chunk_index"] == 1 and payload["page"] == 2 and payload["strategy"] == "recursive"
    assert payload["content"] != "text of chunk 1"
    assert decrypt(payload["content"]) == "text of chunk 1"
    assert decrypt(payload["filename"]) == "report.pdf"
    assert decrypt(payload["section_title"]) == "Title 1"


def test_a_chunk_without_a_title_stores_none(fake):
    store()

    assert fake.points[qdrant_docs.chunk_point_id("u1", "h" * 64, 0)]["payload"]["section_title"] is None


def test_running_again_replaces_instead_of_duplicating(fake):
    store()
    store()

    assert len(fake.points) == 3


def test_mark_ready_flips_only_that_users_document(fake):
    store(user="u1", doc="d1")
    store(user="u2", doc="d1", file_hash="h" * 64)

    qdrant_docs.mark_ready("u1", "d1")

    statuses = {p["payload"]["user_id"]: p["payload"]["status"] for p in fake.points.values()}
    assert statuses == {"u1": "ready", "u2": "processing"}


def test_delete_removes_only_that_users_document(fake):
    store(user="u1", doc="d1")
    store(user="u2", doc="d1")
    store(user="u1", doc="d2", file_hash="g" * 64)

    qdrant_docs.delete_document("u1", "d1")

    left = {(p["payload"]["user_id"], p["payload"]["document_id"]) for p in fake.points.values()}
    assert left == {("u2", "d1"), ("u1", "d2")}


def test_delete_twice_and_delete_without_a_collection_are_fine(fake):
    store()
    qdrant_docs.delete_document("u1", "d1")
    qdrant_docs.delete_document("u1", "d1")

    fake.exists = False
    qdrant_docs.delete_document("u1", "d1")
