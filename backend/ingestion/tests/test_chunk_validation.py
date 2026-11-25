import pytest

import limits
from analysis_schema import DEFAULT_PARAMS, Analysis, default_analysis
from chunk_helpers import make_doc, params, prose, words
from chunking import run_chunking
from chunking.validate import ChunkingFailed, TooManyChunks, find_problem
from doc_model import Block, Chunk


def analysis(strategy, **overrides):
    values = dict(DEFAULT_PARAMS)
    values.update(overrides)
    return Analysis(
        strategy=strategy, params=values, structure="unstructured", document_type="",
        characteristics=[], reasoning="", source="llm", requested_strategy=strategy,
        params_requested={"chunk_size": values["chunk_size"], "chunk_overlap": values["chunk_overlap"]},
    )


def chunk(text, tokens=None, title=None, spans=None):
    return Chunk(0, text, len(text.split()) if tokens is None else tokens, title, None, spans or [(0, 0, len(text))])


# ---------- find_problem ----------

def test_good_chunks_pass():
    blocks = [Block("paragraph", words(100))]
    chunks = [chunk(blocks[0].text)]

    assert find_problem(chunks, blocks, params()) is None


def test_an_empty_chunk_is_a_problem():
    blocks = [Block("paragraph", words(100))]

    assert find_problem([chunk("   ", tokens=0), chunk(blocks[0].text)], blocks, params()) == "empty_chunk"


def test_a_chunk_over_480_tokens_is_a_problem():
    blocks = [Block("paragraph", words(500))]

    assert find_problem([chunk(blocks[0].text)], blocks, params()) == "oversize_chunk"


def test_no_chunks_is_a_problem():
    assert find_problem([], [Block("paragraph", "x")], params()) == "no_chunks"


def test_more_than_5_percent_small_chunks_is_a_problem():
    blocks = [Block("paragraph", words(300))]
    small = [chunk("a b c", title="x") for _ in range(10)]

    assert find_problem(small + [chunk(blocks[0].text, title="x")], blocks, params()) == "too_many_small"


def test_the_last_small_chunk_of_a_section_is_allowed():
    blocks = [Block("paragraph", words(300))]
    chunks = [chunk(words(100), title="A"), chunk("tiny", title="A"), chunk(words(100), title="B"), chunk("end", title="B")]
    chunks[0].spans = [(0, 0, 100)]

    # "tiny" is the last chunk of section A, "end" the last of B: neither counts
    assert find_problem(chunks, blocks, params()) != "too_many_small"


def test_low_coverage_is_a_problem():
    blocks = [Block("paragraph", words(200))]
    half = chunk(words(100), spans=[(0, 0, len(words(100)))])

    assert find_problem([half], blocks, params()) == "low_coverage"


def test_too_many_chunks_raises(monkeypatch):
    monkeypatch.setattr(limits, "MAX_CHUNKS_PER_DOCUMENT", 3)
    blocks = [Block("paragraph", words(10))]

    with pytest.raises(TooManyChunks) as error:
        find_problem([chunk(blocks[0].text)] * 4, blocks, params())

    assert error.value.code == "too_many_chunks"


# ---------- run_chunking ----------

def long_doc():
    return make_doc([
        Block("heading", "A", level=1), Block("paragraph", prose(30, 12, "a")),
        Block("heading", "B", level=1), Block("paragraph", prose(30, 12, "b")),
    ])


def test_run_chunking_uses_the_chosen_strategy_and_reports_it():
    result = run_chunking(long_doc(), analysis("heading_aware", chunk_overlap=0))

    assert result.report["used_strategy"] == "heading_aware"
    assert result.report["fallback_reason"] == ""
    assert result.report["chunk_count"] == len(result.chunks)
    assert [c.index for c in result.chunks] == list(range(len(result.chunks)))
    assert set(result.report["token_stats"]) == {"min", "median", "max"}
    assert result.report["params_used"]["chunk_size"] == 350


def test_a_strategy_that_cannot_run_falls_back_to_recursive_with_the_reason():
    doc = make_doc([Block("paragraph", words(600))])

    result = run_chunking(doc, analysis("single_chunk"))

    assert result.report["requested_strategy"] == "single_chunk"
    assert result.report["used_strategy"] == "recursive"
    assert result.report["fallback_reason"] == "too_long_for_single"
    assert result.report["params_used"]["chunk_overlap"] == 50


def test_a_semantic_document_that_is_too_big_falls_back(monkeypatch):
    monkeypatch.setattr(limits, "SEMANTIC_MAX_CHARS", 100)

    result = run_chunking(long_doc(), analysis("semantic"))

    assert result.report["used_strategy"] == "recursive"
    assert result.report["fallback_reason"] == "semantic_too_large"


def test_chunks_that_fail_the_checks_fall_back_to_recursive(monkeypatch):
    from chunking import STRATEGIES

    def broken(blocks, settings):
        return [chunk("only a little text", title=None, spans=[(0, 0, 5)])]

    monkeypatch.setitem(STRATEGIES, "fixed_size", broken)

    result = run_chunking(long_doc(), analysis("fixed_size"))

    assert result.report["used_strategy"] == "recursive"
    assert result.report["fallback_reason"] == "invalid_chunks:low_coverage"


def test_the_analysis_reason_and_the_new_reason_are_both_kept(monkeypatch):
    doc = make_doc([Block("paragraph", words(600))])
    first = analysis("single_chunk")
    first.fallback_reason = "guard:something"

    result = run_chunking(doc, first)

    assert result.report["fallback_reason"] == "guard:something;too_long_for_single"


def test_if_even_recursive_fails_the_job_cannot_continue(monkeypatch):
    from chunking import STRATEGIES, strategies

    nothing = lambda blocks, settings, indices=None: []
    monkeypatch.setattr(strategies, "recursive", nothing)
    monkeypatch.setitem(STRATEGIES, "recursive", nothing)

    with pytest.raises(ChunkingFailed):
        run_chunking(long_doc(), analysis("recursive"))


def test_too_many_chunks_fails_without_a_fallback(monkeypatch):
    monkeypatch.setattr(limits, "MAX_CHUNKS_PER_DOCUMENT", 2)
    doc = make_doc([Block("paragraph", words(3000))])

    with pytest.raises(TooManyChunks):
        run_chunking(doc, analysis("fixed_size", chunk_size=100, chunk_overlap=0))


def test_a_short_document_becomes_one_chunk():
    doc = make_doc([Block("paragraph", "A short note about nothing much.")])

    result = run_chunking(doc, default_analysis({"tokens": 6, "blocks": {}}))

    assert len(result.chunks) == 1
    assert result.report["used_strategy"] == "single_chunk"
