"""retrieve_messages must only search one user's messages in one chat, and
only return messages that are relevant enough.

The embedding models and the Qdrant server are replaced by fakes before
retrieval.py is imported, so this test needs neither a GPU, a model
download, nor a running Qdrant.
"""

import sys
import types

import pytest

# ---- Fakes ----

# The question is the vector [1, 0], so a stored vector [s, 0] has a
# similarity of exactly s to it.
def fake_embed_query(text):
    return {
        "dense": [1.0, 0.0],
        "sparse": {"indices": [1], "values": [1.0]},
    }


class FakeClient:
    def __init__(self):
        self.calls = []
        self.points = []

    def query_points(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(points=self.points)


fake_client = FakeClient()

# Import retrieval with fake embeddings / Qdrant modules, so this file needs
# no model download and no Qdrant server. The sys.modules entries are put
# back afterwards, so other test files (which import the real service
# modules) are not affected by these fakes.
_names = ["embeddings", "qdrant_store", "retrieval"]
_saved = {name: sys.modules.get(name) for name in _names}

_fake_embeddings = types.ModuleType("embeddings")
_fake_embeddings.embed_query = fake_embed_query
_fake_store = types.ModuleType("qdrant_store")
_fake_store.client = fake_client
_fake_store.COLLECTION_NAME = "chat_messages"

try:
    sys.modules["embeddings"] = _fake_embeddings
    sys.modules["qdrant_store"] = _fake_store
    sys.modules.pop("retrieval", None)

    import retrieval  # noqa: E402
finally:
    for _name in _names:
        if _saved[_name] is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _saved[_name]


def point(point_id, similarity, fusion_score=0.03):
    """A Qdrant hit whose stored vector has `similarity` to the question."""
    return types.SimpleNamespace(
        id=point_id,
        score=fusion_score,
        payload={"content": f"message {point_id}"},
        vector={"dense": [similarity, 0.0]},
    )


@pytest.fixture(autouse=True)
def reset_fake(monkeypatch):
    fake_client.calls.clear()
    fake_client.points = []
    # Whichever copy of retrieval this is, make it use the fakes
    monkeypatch.setattr(retrieval, "client", fake_client)
    monkeypatch.setattr(retrieval, "embed_query", fake_embed_query)
    monkeypatch.setattr(retrieval, "MIN_SIMILARITY", 0.5)


def search(**kwargs):
    return retrieval.retrieve_messages(
        "hi", user_id="u1", conversation_id="c1", **kwargs
    )


def conditions(query_filter):
    return {c.key: c.match.value for c in query_filter.must}


# ---------- one user, one chat ----------

def test_filter_has_both_the_user_and_the_chat():
    search()

    assert conditions(fake_client.calls[0]["query_filter"]) == {
        "user_id": "u1",
        "conversation_id": "c1",
    }


def test_both_prefetch_searches_use_the_same_filter():
    search()

    call = fake_client.calls[0]

    assert len(call["prefetch"]) == 2
    for prefetch in call["prefetch"]:
        assert conditions(prefetch.filter) == {
            "user_id": "u1",
            "conversation_id": "c1",
        }


@pytest.mark.parametrize(
    "user_id, conversation_id",
    [("", "c1"), ("u1", ""), ("", "")],
)
def test_missing_id_is_refused_and_nothing_is_searched(user_id, conversation_id):
    with pytest.raises(ValueError):
        retrieval.retrieve_messages(
            "hi", user_id=user_id, conversation_id=conversation_id
        )

    assert fake_client.calls == []


# ---------- relevance threshold ----------

def test_weak_matches_are_dropped():
    fake_client.points = [point("a", 0.9), point("b", 0.3), point("c", 0.6)]

    assert [r["id"] for r in search()] == ["a", "c"]


def test_a_match_exactly_at_the_threshold_is_kept():
    fake_client.points = [point("a", 0.5)]

    assert [r["id"] for r in search()] == ["a"]


def test_nothing_relevant_gives_an_empty_list():
    fake_client.points = [point("a", 0.2), point("b", 0.1)]

    assert search() == []


def test_score_is_the_similarity_not_the_fusion_score():
    fake_client.points = [point("a", 0.8, fusion_score=0.031)]

    assert search()[0]["score"] == pytest.approx(0.8)


def test_hybrid_order_is_kept():
    # "b" is more similar, but "a" ranked higher in the hybrid search
    fake_client.points = [point("a", 0.6), point("b", 0.9)]

    assert [r["id"] for r in search()] == ["a", "b"]


def test_result_is_cut_to_the_limit_after_weak_ones_are_dropped():
    fake_client.points = [
        point("weak", 0.1),
        point("a", 0.9),
        point("b", 0.8),
        point("c", 0.7),
    ]

    assert [r["id"] for r in search(limit=2)] == ["a", "b"]


def test_a_hit_without_a_stored_vector_is_dropped():
    hit = point("a", 0.9)
    hit.vector = None
    fake_client.points = [hit, point("b", 0.9)]

    assert [r["id"] for r in search()] == ["b"]


def test_min_similarity_argument_overrides_the_default():
    fake_client.points = [point("a", 0.6)]

    assert search(min_similarity=0.9) == []
    assert [r["id"] for r in search(min_similarity=0.1)] == ["a"]


def test_qdrant_is_asked_for_dense_vectors_and_extra_candidates():
    search(limit=5)

    call = fake_client.calls[0]

    assert call["with_vectors"] == ["dense"]
    assert call["limit"] == 5 * retrieval.CANDIDATE_FACTOR
    assert all(p.limit == call["limit"] for p in call["prefetch"])