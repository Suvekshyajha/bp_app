"""Moving stored vectors to a new dense model, on a REAL (in-process) Qdrant.
Only the embedding function is a stand-in."""

import warnings

import pytest
from qdrant_client import QdrantClient, models

import reembed_collections
from common.crypto import encrypt
from common.settings import CHUNKS_COLLECTION, DENSE_DIM, MESSAGES_COLLECTION

MESSAGE = "11111111-1111-4111-8111-111111111111"
OLD_PLAIN = "22222222-2222-4222-8222-222222222222"
TITLED = "33333333-3333-4333-8333-333333333333"
UNTITLED = "44444444-4444-4444-8444-444444444444"


class FakeEmbed:
    def __init__(self):
        self.texts = []

    def __call__(self, texts):
        self.texts.extend(texts)
        return [
            {"dense": [1.0] + [0.0] * (DENSE_DIM - 1), "sparse": {"indices": [7], "values": [0.5]}}
            for _ in texts
        ]


def _old_collection(client, name, points):
    client.create_collection(
        collection_name=name,
        vectors_config={"dense": models.VectorParams(size=4, distance=models.Distance.COSINE)},
        sparse_vectors_config={"sparse": models.SparseVectorParams()},
    )
    client.upsert(
        collection_name=name,
        points=[
            models.PointStruct(
                id=point_id,
                vector={"dense": [1.0, 0.0, 0.0, 0.0], "sparse": models.SparseVector(indices=[1], values=[1.0])},
                payload=payload,
            )
            for point_id, payload in points
        ],
    )


@pytest.fixture(autouse=True)
def quiet():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # local mode has no payload indexes
        yield


@pytest.fixture
def store():
    client = QdrantClient(":memory:")
    _old_collection(client, "chat_messages", [
        (MESSAGE, {"user_id": "u1", "conversation_id": "c1", "content": encrypt("my dog is Bruno")}),
        (OLD_PLAIN, {"user_id": "u1", "conversation_id": "c1", "content": "saved before encryption"}),
    ])
    _old_collection(client, "document_chunks", [
        (TITLED, {"user_id": "u1", "document_id": "d1", "status": "ready",
                  "section_title": encrypt("Dosage"), "content": encrypt("take one tablet")}),
        (UNTITLED, {"user_id": "u1", "document_id": "d1", "status": "ready",
                    "section_title": None, "content": encrypt("keep it dry")}),
    ])
    return client


def _all(client, name):
    points, _ = client.scroll(collection_name=name, limit=100, with_payload=True, with_vectors=True)
    return {str(point.id): point for point in points}


def test_every_point_is_copied_with_new_vectors_and_the_same_payload(store):
    copied = reembed_collections.reembed(store, FakeEmbed())

    assert copied == {MESSAGES_COLLECTION: 2, CHUNKS_COLLECTION: 2}
    for old_name, new_name in (("chat_messages", MESSAGES_COLLECTION), ("document_chunks", CHUNKS_COLLECTION)):
        old, new = _all(store, old_name), _all(store, new_name)
        assert new.keys() == old.keys()
        for point_id, point in new.items():
            assert point.payload == old[point_id].payload  # still encrypted
            assert len(point.vector["dense"]) == DENSE_DIM
            assert point.vector["sparse"].indices == [7]


def test_the_model_reads_the_same_text_as_when_it_was_first_stored(store):
    embed = FakeEmbed()

    reembed_collections.reembed(store, embed)

    # messages: the text. chunks: the section title, a new line, then the text
    assert sorted(embed.texts) == sorted([
        "my dog is Bruno", "saved before encryption",
        "Dosage\ntake one tablet", "keep it dry",
    ])


def test_running_again_only_adds_what_is_missing(store):
    reembed_collections.reembed(store, FakeEmbed())
    store.delete(collection_name=MESSAGES_COLLECTION, points_selector=models.PointIdsList(points=[MESSAGE]))
    embed = FakeEmbed()

    copied = reembed_collections.reembed(store, embed)

    assert copied == {MESSAGES_COLLECTION: 1, CHUNKS_COLLECTION: 0}
    assert embed.texts == ["my dog is Bruno"]


def test_more_points_than_one_batch_are_all_copied(store, monkeypatch):
    monkeypatch.setattr(reembed_collections, "BATCH_SIZE", 1)

    copied = reembed_collections.reembed(store, FakeEmbed())

    assert copied == {MESSAGES_COLLECTION: 2, CHUNKS_COLLECTION: 2}


def test_nothing_to_move_is_not_an_error():
    client = QdrantClient(":memory:")

    assert reembed_collections.reembed(client, FakeEmbed()) == {MESSAGES_COLLECTION: 0, CHUNKS_COLLECTION: 0}
    assert not client.collection_exists(MESSAGES_COLLECTION)
