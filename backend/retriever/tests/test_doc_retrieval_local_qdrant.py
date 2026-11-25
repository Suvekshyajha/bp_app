"""doc_retrieval against a REAL Qdrant engine (qdrant-client's in-process mode).

The other tests use a hand-made fake client, which cannot tell whether the
filters and queries are valid Qdrant calls. Here the real engine runs them:
hybrid search with fusion, filtered scrolls and all. No server and no
embedding model is needed: vectors are made by hand, one axis per topic.

The scenario is the one the feature is for: three documents are uploaded, the
question does not name any of them, and the right one must still be found,
without ever touching another user's documents.
"""

import pytest
from qdrant_client import QdrantClient, models

import doc_retrieval
from common.crypto import encrypt
from common.settings import CHUNKS_COLLECTION

ALICE = "alice-id"
BOB = "bob-id"

DOC_COOKING = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DOC_CONTEXT = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
DOC_FINANCE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
DOC_BOBS = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

# One axis per topic (and a fourth axis no document is about). A question about
# a topic points along the same axis, so its cosine similarity with that
# topic's chunks is 1 and with the others 0.
COOKING = [1.0, 0.0, 0.0, 0.0]
CONTEXT = [0.0, 1.0, 0.0, 0.0]
FINANCE = [0.0, 0.0, 1.0, 0.0]
# Mostly the fourth axis: about 0.29 similar to each topic, under the 0.5 threshold
UNRELATED = [0.289, 0.289, 0.289, 0.866]


def question(dense, term):
    return {
        "dense": dense,
        "sparse": {"indices": [term], "values": [1.0]},
    }


def chunk_point(id, user, document, filename, index, dense, term, text, status="ready"):
    return models.PointStruct(
        id=id,
        vector={
            "dense": dense,
            "sparse": models.SparseVector(indices=[term], values=[1.0]),
        },
        payload={
            "user_id": user,
            "document_id": document,
            "chunk_index": index,
            "filename": encrypt(filename),
            "section_title": None,
            "page": index + 1,
            "content": encrypt(text),
            "status": status,
            "created_at": "2026-11-01T10:00:00+00:00",
        },
    )


@pytest.fixture
def engine(monkeypatch):
    """A real (in-process) Qdrant holding Alice's three documents and Bob's one."""

    local = QdrantClient(":memory:")
    local.create_collection(
        collection_name=CHUNKS_COLLECTION,
        vectors_config={"dense": models.VectorParams(size=4, distance=models.Distance.COSINE)},
        sparse_vectors_config={"sparse": models.SparseVectorParams()},
    )

    points = []
    number = 0

    def add(user, document, filename, count, dense, term, text, status="ready"):
        nonlocal number
        for index in range(count):
            number += 1
            points.append(
                chunk_point(
                    number, user, document, filename, index, dense, term,
                    f"{text} part {index}", status,
                )
            )

    add(ALICE, DOC_COOKING, "Document_A.pdf", 3, COOKING, 10, "Recipes and cooking")
    add(ALICE, DOC_CONTEXT, "Document_B.pdf", 5, CONTEXT, 20, "Context window management")
    add(ALICE, DOC_FINANCE, "Document_C.pdf", 3, FINANCE, 30, "Quarterly finance")
    # Bob's document is about the very same topic as Alice's Document_B
    add(BOB, DOC_BOBS, "bobs_secret.pdf", 4, CONTEXT, 20, "Bob's private context notes")

    local.upsert(collection_name=CHUNKS_COLLECTION, points=points)

    monkeypatch.setattr(doc_retrieval, "client", local)
    monkeypatch.setattr(doc_retrieval, "_collection_exists", lambda: True)

    return local


def documents_of(results):
    return {r["payload"]["document_id"] for r in results}


def test_the_right_document_is_found_without_naming_it(engine):
    """'What does the document say about context window management?'"""

    results = doc_retrieval.retrieve_document_chunks(
        question(CONTEXT, 20), ALICE, None
    )

    assert results, "nothing was found"
    assert documents_of(results) == {DOC_CONTEXT}
    assert {r["payload"]["filename"] for r in results} == {"Document_B.pdf"}
    assert all("Context window management" in r["payload"]["content"] for r in results)


def test_each_topic_finds_its_own_document(engine):
    for topic, term, document in [
        (COOKING, 10, DOC_COOKING),
        (CONTEXT, 20, DOC_CONTEXT),
        (FINANCE, 30, DOC_FINANCE),
    ]:
        results = doc_retrieval.retrieve_document_chunks(question(topic, term), ALICE, None)

        assert documents_of(results) == {document}


def test_another_users_documents_are_never_returned(engine):
    """Bob has chunks about exactly the same topic. Alice must never get them."""

    results = doc_retrieval.retrieve_document_chunks(question(CONTEXT, 20), ALICE, None)

    assert DOC_BOBS not in documents_of(results)
    assert not any("Bob" in r["payload"]["content"] for r in results)

    # and the other way round
    bobs = doc_retrieval.retrieve_document_chunks(question(CONTEXT, 20), BOB, None)

    assert documents_of(bobs) == {DOC_BOBS}


def test_naming_someone_elses_document_id_finds_nothing(engine):
    assert doc_retrieval.retrieve_document_chunks(
        question(CONTEXT, 20), ALICE, [DOC_BOBS]
    ) == []


def test_a_question_no_document_covers_finds_nothing(engine):
    """No document is guessed when nothing is relevant."""

    results = doc_retrieval.retrieve_document_chunks(question(UNRELATED, 99), ALICE, None)

    assert results == []


def test_the_chunks_around_a_hit_come_along(engine):
    """An answer that runs over a chunk border is not cut in half.

    Only chunk 2 of Document_B matches a very specific term here; chunks 1 and
    3 (its neighbours) do not, but they are added as its surrounding text.
    """

    points = engine.scroll(
        CHUNKS_COLLECTION,
        scroll_filter=models.Filter(must=[
            models.FieldCondition(key="document_id", match=models.MatchValue(value=DOC_CONTEXT)),
            models.FieldCondition(key="chunk_index", match=models.MatchValue(value=2)),
        ]),
        limit=1,
    )[0]
    middle = points[0]

    # make chunk 2 the only close one: its neighbours now point elsewhere
    engine.update_vectors(
        CHUNKS_COLLECTION,
        points=[
            models.PointVectors(id=p.id, vector={"dense": FINANCE})
            for p in engine.scroll(
                CHUNKS_COLLECTION,
                scroll_filter=models.Filter(must=[
                    models.FieldCondition(key="document_id", match=models.MatchValue(value=DOC_CONTEXT)),
                    models.FieldCondition(
                        key="chunk_index", match=models.MatchAny(any=[0, 1, 3, 4])
                    ),
                ]),
                limit=10,
            )[0]
        ],
    )

    results = doc_retrieval.retrieve_document_chunks(
        question(CONTEXT, 20), ALICE, [DOC_CONTEXT], limit=1
    )

    indexes = [r["payload"]["chunk_index"] for r in results]
    assert indexes[0] == 2                      # the hit itself first
    assert sorted(indexes) == [1, 2, 3]         # then the chunks next to it
    assert results[0]["id"] == str(middle.id)


def test_documents_that_are_still_processing_are_not_searched(engine):
    engine.upsert(
        collection_name=CHUNKS_COLLECTION,
        points=[
            chunk_point(
                1000, ALICE, "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", "half_done.pdf",
                0, CONTEXT, 20, "unfinished", status="processing",
            )
        ],
    )

    results = doc_retrieval.retrieve_document_chunks(question(CONTEXT, 20), ALICE, None)

    assert "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee" not in documents_of(results)
    assert "half_done.pdf" not in [d["filename"] for d in doc_retrieval.list_documents(ALICE)]


def test_the_library_lists_each_ready_document_once_and_only_the_users(engine):
    listed = doc_retrieval.list_documents(ALICE)

    assert [d["filename"] for d in listed] == ["Document_A.pdf", "Document_B.pdf", "Document_C.pdf"]
    assert [d["document_id"] for d in listed] == [DOC_COOKING, DOC_CONTEXT, DOC_FINANCE]
    assert [d["filename"] for d in doc_retrieval.list_documents(BOB)] == ["bobs_secret.pdf"]
    assert doc_retrieval.list_documents("nobody") == []


def test_a_question_that_touches_two_documents_uses_both(engine):
    """Half cooking, half finance: neither document crowds the other out."""

    blended = [0.7071, 0.0, 0.7071, 0.0]
    query = {
        "dense": blended,
        "sparse": {"indices": [10, 30], "values": [1.0, 1.0]},
    }

    results = doc_retrieval.retrieve_document_chunks(
        query, ALICE, None, limit=4, min_similarity=0.5
    )

    assert {DOC_COOKING, DOC_FINANCE} <= documents_of(results)
