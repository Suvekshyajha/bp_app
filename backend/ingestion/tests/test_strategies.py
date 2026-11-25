import pytest

import limits
from chunk_helpers import assert_well_formed, make_doc, params, prose, words
from chunking import strategies
from chunking.strategies import ChunkingFallback, TextView, sentence_ranges
from doc_model import Block


def para(n_words, prefix="w"):
    return Block("paragraph", words(n_words, prefix))


# ---------- single_chunk ----------

def test_single_chunk_keeps_everything_in_one_chunk():
    doc = make_doc([Block("heading", "Title", level=1), para(50)])

    chunks = strategies.single_chunk(doc.blocks, params())

    assert len(chunks) == 1
    assert "Title" in chunks[0].text and "w49" in chunks[0].text
    assert_well_formed(chunks, doc.blocks)


def test_single_chunk_is_refused_above_450_tokens():
    doc = make_doc([para(460)])

    with pytest.raises(ChunkingFallback) as error:
        strategies.single_chunk(doc.blocks, params())

    assert error.value.reason == "too_long_for_single"


# ---------- fixed_size ----------

def test_fixed_size_makes_chunks_of_the_size_with_overlap():
    doc = make_doc([para(1000)])

    chunks = strategies.fixed_size(doc.blocks, params(chunk_size=200, chunk_overlap=50))

    assert_well_formed(chunks, doc.blocks)
    assert all(c.tokens <= 200 for c in chunks)
    assert chunks[0].tokens == 200
    # the second chunk starts 50 words before the first one ended
    first_words = chunks[0].text.split()
    assert chunks[1].text.split()[:50] == first_words[-50:]


def test_fixed_size_never_cuts_inside_a_word():
    doc = make_doc([para(500)])

    for chunk in strategies.fixed_size(doc.blocks, params(chunk_size=120, chunk_overlap=20)):
        for token in chunk.text.split():
            assert token.startswith("w") and token[1:].isdigit()


# ---------- sentence ----------

def test_sentences_stay_whole():
    doc = make_doc([Block("paragraph", prose(40, 10))])

    chunks = strategies.sentence(doc.blocks, params(chunk_size=100, chunk_overlap=0))

    assert_well_formed(chunks, doc.blocks)
    for chunk in chunks:
        assert chunk.text.rstrip().endswith(".")
        assert chunk.text[0].isupper()


def test_sentence_overlap_is_whole_trailing_sentences():
    doc = make_doc([Block("paragraph", prose(40, 10))])

    chunks = strategies.sentence(doc.blocks, params(chunk_size=100, chunk_overlap=20))

    shared = set(chunks[0].text.split(". ")) & set(chunks[1].text.split(". "))
    assert shared


def test_the_sentence_splitter_protects_abbreviations():
    text = "See Fig. 3 for details, e.g. the plot. Dr. Smith agrees. It ends here."

    parts = [text[a:b] for a, b in sentence_ranges(text)]

    assert parts == [
        "See Fig. 3 for details, e.g. the plot.",
        "Dr. Smith agrees.",
        "It ends here.",
    ]


def test_a_sentence_longer_than_the_maximum_is_split():
    doc = make_doc([Block("paragraph", words(900) + ".")])

    chunks = strategies.sentence(doc.blocks, params(chunk_size=300, chunk_overlap=0, max_chunk_size=400))

    assert len(chunks) >= 3
    assert_well_formed(chunks, doc.blocks, maximum=400)


# ---------- paragraph ----------

def test_paragraphs_are_packed_whole_without_overlap():
    doc = make_doc([para(120, f"p{i}_") for i in range(10)])

    chunks = strategies.paragraph(doc.blocks, params(chunk_size=300, chunk_overlap=40))

    assert_well_formed(chunks, doc.blocks)
    # 120 + 120 fit in 300, a third does not: two paragraphs per chunk, no shared text
    assert len(chunks) == 5
    assert not set(chunks[0].text.split()) & set(chunks[1].text.split())


def test_a_paragraph_above_the_maximum_is_split_by_sentences_and_only_those_overlap():
    big = Block("paragraph", prose(60, 10, "big"))
    doc = make_doc([para(50, "a"), big, para(50, "b")])

    chunks = strategies.paragraph(doc.blocks, params(chunk_size=200, chunk_overlap=30, max_chunk_size=300))

    assert_well_formed(chunks, doc.blocks, maximum=300)
    big_pieces = [c for c in chunks if "big" in c.text]
    assert len(big_pieces) >= 3
    assert set(big_pieces[0].text.split()) & set(big_pieces[1].text.split())


# ---------- recursive ----------

def test_recursive_prefers_paragraph_breaks():
    doc = make_doc([para(150, f"p{i}_") for i in range(4)])

    chunks = strategies.recursive(doc.blocks, params(chunk_size=200, chunk_overlap=0))

    assert_well_formed(chunks, doc.blocks)
    for chunk in chunks:
        # each chunk holds whole paragraphs only
        assert chunk.text.count("p0_0") + chunk.text.count("p1_0") + chunk.text.count("p2_0") + chunk.text.count("p3_0") >= 1
        first_words = {w.split("_")[0] for w in chunk.text.split()}
        assert len(first_words) == 1


def test_recursive_handles_one_giant_paragraph():
    doc = make_doc([para(2000)])

    chunks = strategies.recursive(doc.blocks, params())

    assert_well_formed(chunks, doc.blocks)
    assert all(c.tokens <= 350 for c in chunks)


def test_recursive_cuts_a_single_endless_word_as_a_last_resort():
    doc = make_doc([Block("paragraph", "x" * 5000)])
    import chunking.tokens as tokens
    tokens.set_counter(lambda text: len(text) // 10 + (1 if text else 0))

    chunks = strategies.recursive(doc.blocks, params(chunk_size=100, chunk_overlap=0))

    assert all(c.tokens <= 440 for c in chunks)  # a small tail may join the last piece
    assert len(chunks) > 3


# ---------- sliding_window ----------

def test_sliding_window_forces_at_least_20_percent_overlap():
    doc = make_doc([Block("paragraph", prose(60, 10))])

    chunks = strategies.sliding_window(doc.blocks, params(chunk_size=100, chunk_overlap=0))

    assert_well_formed(chunks, doc.blocks)
    for before, after in zip(chunks, chunks[1:]):
        assert set(before.text.split()) & set(after.text.split())


# ---------- heading_aware ----------

def sectioned():
    return make_doc([
        Block("heading", "Alpha", level=1),
        para(80, "a"),
        Block("heading", "Beta", level=1),
        para(80, "b"),
        Block("heading", "Gamma", level=1),
        para(80, "c"),
    ])


def test_each_section_is_one_chunk_with_its_title():
    doc = sectioned()

    chunks = strategies.heading_aware(doc.blocks, params(chunk_overlap=0))

    assert_well_formed(chunks, doc.blocks)
    assert [c.section_title for c in chunks] == ["Alpha", "Beta", "Gamma"]
    assert chunks[1].text.startswith("Beta")


def test_small_sibling_sections_merge_until_the_minimum():
    doc = make_doc([
        Block("heading", "Guide", level=1),
        para(100, "g"),
        Block("heading", "A", level=2),
        para(10, "a"),
        Block("heading", "B", level=2),
        para(10, "b"),
        Block("heading", "C", level=2),
        para(70, "c"),
    ])

    chunks = strategies.heading_aware(doc.blocks, params(chunk_overlap=0, min_chunk_size=60))

    assert_well_formed(chunks, doc.blocks)
    merged = [c for c in chunks if "a0" in c.text][0]
    assert "b0" in merged.text and "c0" in merged.text  # joined while still under the minimum
    assert len(chunks) == 2
    assert chunks[1].section_title == "Guide > A"


def test_a_tiny_heading_joins_the_big_section_below_it():
    doc = make_doc([
        Block("heading", "Title", level=1),
        Block("heading", "Big", level=2),
        para(900, "z"),
    ])

    chunks = strategies.heading_aware(doc.blocks, params(chunk_overlap=0))

    assert_well_formed(chunks, doc.blocks)
    assert "Title" in chunks[0].text
    assert all(c.tokens >= 60 for c in chunks[:-1])


def test_a_big_section_is_split_with_the_overlap_and_keeps_its_title():
    doc = make_doc([
        Block("heading", "Intro", level=1),
        para(30, "i"),
        Block("heading", "Big", level=1),
        para(1000, "z"),
    ])

    chunks = strategies.heading_aware(doc.blocks, params(chunk_overlap=40))

    assert_well_formed(chunks, doc.blocks)
    big = [c for c in chunks if c.section_title == "Big"]
    assert len(big) >= 3
    assert set(big[0].text.split()) & set(big[1].text.split())


def test_text_before_the_first_heading_is_kept():
    doc = make_doc([para(80, "pre"), Block("heading", "H", level=1), para(80, "h")])

    chunks = strategies.heading_aware(doc.blocks, params(chunk_overlap=0))

    assert_well_formed(chunks, doc.blocks)
    assert chunks[0].section_title is None
    assert chunks[1].section_title == "H"


def test_the_title_is_the_last_two_levels_of_the_path():
    doc = make_doc([
        Block("heading", "Part", level=1),
        para(100, "p"),
        Block("heading", "Chapter", level=2),
        para(100, "c"),
        Block("heading", "Topic", level=3),
        para(100, "t"),
    ])

    chunks = strategies.heading_aware(doc.blocks, params(chunk_overlap=0))

    assert [c.section_title for c in chunks] == ["Part", "Part > Chapter", "Chapter > Topic"]


# ---------- the same for every chunker ----------

ALL = ["fixed_size", "sentence", "paragraph", "recursive", "sliding_window", "heading_aware"]


def mixed_doc():
    blocks = []
    for section in range(6):
        blocks.append(Block("heading", f"Section {section}", level=1))
        blocks.append(Block("paragraph", prose(12, 12, f"p{section}")))
        blocks.append(Block("list", "\n".join(f"- item {section} {i}" for i in range(5))))
        blocks.append(Block("paragraph", prose(3, 20, f"q{section}")))
    return make_doc(blocks)


@pytest.mark.parametrize("name", ALL)
def test_every_chunker_keeps_the_common_rules(name):
    doc = mixed_doc()
    chunker = getattr(strategies, name)

    chunks = chunker(doc.blocks, params(chunk_size=150, chunk_overlap=20, max_chunk_size=200))

    assert_well_formed(chunks, doc.blocks, maximum=200)


@pytest.mark.parametrize("name", ALL)
def test_every_chunker_is_deterministic(name):
    doc = mixed_doc()
    chunker = getattr(strategies, name)
    settings = params(chunk_size=150, chunk_overlap=20, max_chunk_size=200)

    first = [(c.text, c.spans) for c in chunker(doc.blocks, settings)]
    second = [(c.text, c.spans) for c in chunker(doc.blocks, settings)]

    assert first == second


@pytest.mark.parametrize("name", ALL)
def test_every_chunker_handles_one_tiny_block(name):
    doc = make_doc([Block("paragraph", "Just a few words here.")])

    chunks = getattr(strategies, name)(doc.blocks, params())

    assert len(chunks) == 1
    assert chunks[0].text == "Just a few words here."


@pytest.mark.parametrize("name", ALL)
def test_chunks_record_the_page_of_their_first_block(name):
    blocks = [Block("paragraph", words(200, "a"), page=3), Block("paragraph", words(200, "b"), page=4)]
    doc = make_doc(blocks)

    chunks = getattr(strategies, name)(doc.blocks, params(chunk_size=150, chunk_overlap=0))

    assert chunks[0].page == 3
    assert chunks[-1].page in (3, 4)


def test_spans_point_back_at_the_source_text():
    doc = make_doc([para(40, "a"), para(40, "b")])
    chunks = strategies.recursive(doc.blocks, params(chunk_size=500, chunk_overlap=0))

    for block_index, start, end in chunks[0].spans:
        assert doc.blocks[block_index].text[start:end] in chunks[0].text


def test_text_view_maps_positions_to_blocks():
    doc = make_doc([Block("paragraph", "one two"), Block("paragraph", "three")])
    view = TextView(doc.blocks)

    assert view.text == "one two\n\nthree"
    assert view.spans_for(0, 3) == [(0, 0, 3)]
    assert view.spans_for(5, 14) == [(0, 5, 7), (1, 0, 5)]
