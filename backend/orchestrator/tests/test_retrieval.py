"""retrieve_messages must only ever search one user's messages in one chat.

The embedding models and the Qdrant server are replaced by fakes before
retrieval.py is imported, so this test needs neither a GPU, a model
download, nor a running Qdrant.
"""

import sys
import types

import pytest

# ---- Fakes (installed before the module under test is imported) ----

fake_embeddings = types.ModuleType("embeddings")
fake_embeddings.embed_query = lambda text: {
    "dense": [0.1, 0.2],
    "sparse": {"indices": [1], "values": [1.0]},
}
sys.modules["embeddings"] = fake_embeddings


class FakeClient:
    def __init__(self):
        self.calls = []

    def query_points(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(points=[])


fake_client = FakeClient()
fake_store = types.ModuleType("qdrant_store")
fake_store.client = fake_client
fake_store.COLLECTION_NAME = "chat_messages"
sys.modules["qdrant_store"] = fake_store

import retrieval  # noqa: E402


@pytest.fixture(autouse=True)
def reset_calls():
    fake_client.calls.clear()


def conditions(query_filter):
    return {c.key: c.match.value for c in query_filter.must}


def test_filter_has_both_the_user_and_the_chat():
    retrieval.retrieve_messages("hi", user_id="u1", conversation_id="c1")

    call = fake_client.calls[0]

    assert conditions(call["query_filter"]) == {
        "user_id": "u1",
        "conversation_id": "c1",
    }


def test_both_prefetch_searches_use_the_same_filter():
    retrieval.retrieve_messages("hi", user_id="u1", conversation_id="c1")

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