"""How the dense model is loaded and what text it is given. No real model:
sentence_transformers is replaced, so nothing is downloaded."""

import sys
import types

import pytest

from common import embeddings, settings


class _Vector(list):
    def tolist(self):
        return list(self)


class FakeSentenceTransformer:
    def __init__(self, name, **kwargs):
        self.name = name
        self.kwargs = kwargs
        self.max_seq_length = 8192
        self.encoded = []

    def encode(self, texts, **kwargs):
        self.encoded.append(texts)
        return _Vector([0.0])


class FakeSparse:
    def query_embed(self, text):
        yield types.SimpleNamespace(indices=_Vector([1]), values=_Vector([1.0]))


@pytest.fixture
def fake_library(monkeypatch):
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(embeddings, "_dense", None)
    monkeypatch.setattr(embeddings, "_sparse", FakeSparse())


def test_the_dense_model_is_granite_small_with_384_numbers():
    assert settings.DENSE_MODEL == "ibm-granite/granite-embedding-small-english-r2"
    assert settings.DENSE_DIM == 384


def test_the_model_is_loaded_as_float32(fake_library):
    """Its weights are saved as bfloat16, which is several times slower on CPU."""

    model = embeddings.get_dense()

    assert model.name == settings.DENSE_MODEL
    assert model.kwargs["model_kwargs"] == {"dtype": "float32"}


def test_a_very_long_text_is_cut_so_one_message_cannot_stall_the_cpu(fake_library):
    assert embeddings.get_dense().max_seq_length == 2048


def test_a_query_is_embedded_without_an_instruction_prefix(fake_library):
    embeddings.embed_query("what is my dog called?")

    assert embeddings.get_dense().encoded == ["what is my dog called?"]
