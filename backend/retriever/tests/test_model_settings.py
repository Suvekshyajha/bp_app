"""The retriever's settings that depend on which dense model is used."""

import importlib.util
import warnings
from pathlib import Path

import dotenv
import pytest
from qdrant_client import QdrantClient

import qdrant_store
from common.settings import DENSE_DIM, MESSAGES_COLLECTION

SERVICE = Path(__file__).resolve().parent.parent


def test_messages_go_to_a_new_collection_sized_for_the_model(monkeypatch):
    """Vectors of two models must never share a collection, so the model change
    comes with a new collection name."""

    local = QdrantClient(":memory:")
    monkeypatch.setattr(qdrant_store, "client", local)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # local mode has no payload indexes
        qdrant_store.create_collection()

    assert qdrant_store.COLLECTION_NAME == MESSAGES_COLLECTION == "chat_messages_v3"
    info = local.get_collection(MESSAGES_COLLECTION)
    assert info.config.params.vectors["dense"].size == DENSE_DIM


def _fresh(name):
    """Load a module again, under another name, so its defaults can be read."""

    spec = importlib.util.spec_from_file_location(f"{name}_defaults", SERVICE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def no_tuning(monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.delenv("RETRIEVAL_MIN_SIMILARITY", raising=False)
    monkeypatch.delenv("RETRIEVAL_SELECTED_MIN_SIMILARITY", raising=False)


def test_default_thresholds_without_any_tuning(no_tuning):
    """The score scale belongs to the model. A change of model must come with
    a deliberate change of these defaults."""

    assert _fresh("retrieval").MIN_SIMILARITY == 0.5
    documents = _fresh("doc_retrieval")
    assert documents.MIN_SIMILARITY == 0.5
    assert documents.SELECTED_MIN_SIMILARITY == 0.2
