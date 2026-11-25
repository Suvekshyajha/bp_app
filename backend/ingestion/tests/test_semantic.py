import pytest

import limits
from chunk_helpers import assert_well_formed, make_doc, params, prose
from chunking import semantic as semantic_module
from chunking.semantic import semantic
from chunking.strategies import ChunkingFallback
from doc_model import Block

TOPICS = {
    "cat": [1.0, 0.0, 0.0],
    "sea": [0.0, 1.0, 0.0],
    "law": [0.0, 0.0, 1.0],
}


def topic_text():
    """Three topics of 12 sentences each. Each sentence says its topic word."""

    sentences = []
    for topic in ("cat", "sea", "law"):
        for number in range(12):
            sentences.append(f"The {topic} sentence {number} has " + " ".join(f"{topic}w{i}" for i in range(14)) + ".")
    return " ".join(sentences)


def topic_embedder(texts):
    vectors = []
    for text in texts:
        counts = {topic: text.count(f"The {topic} ") for topic in TOPICS}
        best = max(counts, key=counts.get)
        vectors.append({"dense": list(TOPICS[best])})
    return vectors


@pytest.fixture(autouse=True)
def fake_embedder():
    semantic_module.set_embedder(topic_embedder)
    yield
    semantic_module.set_embedder(None)


def test_cuts_happen_where_the_topic_changes():
    doc = make_doc([Block("paragraph", topic_text())])

    chunks = semantic(doc.blocks, params(chunk_size=350, min_chunk_size=60, max_chunk_size=440))

    assert_well_formed(chunks, doc.blocks)
    assert len(chunks) == 3
    assert "The cat " in chunks[0].text and "The sea " not in chunks[0].text
    assert "The sea " in chunks[1].text and "The law " not in chunks[1].text
    assert "The law " in chunks[2].text


def test_a_segment_never_passes_the_maximum():
    doc = make_doc([Block("paragraph", topic_text())])

    chunks = semantic(doc.blocks, params(chunk_size=150, min_chunk_size=40, max_chunk_size=150))

    assert_well_formed(chunks, doc.blocks, maximum=150)
    assert len(chunks) > 3


def test_tiny_segments_join_a_neighbour():
    text = prose(3, 5, "cat") + " " + prose(30, 12, "sea")
    doc = make_doc([Block("paragraph", text)])
    semantic_module.set_embedder(lambda texts: [{"dense": [1.0, 0.0] if "cat" in t[:12] else [0.0, 1.0]} for t in texts])

    chunks = semantic(doc.blocks, params(min_chunk_size=60))

    assert_well_formed(chunks, doc.blocks)
    assert all(c.tokens >= 60 for c in chunks[:-1])


def test_a_document_over_the_size_cap_falls_back(monkeypatch):
    monkeypatch.setattr(limits, "SEMANTIC_MAX_CHARS", 500)
    doc = make_doc([Block("paragraph", topic_text())])

    with pytest.raises(ChunkingFallback) as error:
        semantic(doc.blocks, params())

    assert error.value.reason == "semantic_too_large"


def test_too_many_sentences_falls_back(monkeypatch):
    monkeypatch.setattr(limits, "SEMANTIC_MAX_SENTENCES", 10)
    doc = make_doc([Block("paragraph", topic_text())])

    with pytest.raises(ChunkingFallback):
        semantic(doc.blocks, params())


def test_a_document_with_fewer_than_three_sentences_just_uses_recursive():
    doc = make_doc([Block("paragraph", "One sentence only. And a second one.")])

    chunks = semantic(doc.blocks, params())

    assert len(chunks) == 1


def test_the_same_input_gives_the_same_chunks():
    doc = make_doc([Block("paragraph", topic_text())])

    first = [c.text for c in semantic(doc.blocks, params())]
    second = [c.text for c in semantic(doc.blocks, params())]

    assert first == second
