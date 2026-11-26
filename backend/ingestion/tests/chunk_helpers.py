"""Helpers for the chunking tests (documents are made from numbered words)."""

from analysis_schema import DEFAULT_PARAMS
from doc_model import Block, ParsedDocument, assign_section_paths
from chunking.validate import _coverage


def words(count, prefix="w"):
    return " ".join(f"{prefix}{i}" for i in range(count))


def make_doc(blocks, source_type="md"):
    assign_section_paths(blocks)
    return ParsedDocument(blocks, source_type, None)


def params(**overrides):
    result = dict(DEFAULT_PARAMS)
    result.update(overrides)
    return result


def prose(sentences, words_each=10, prefix="s"):
    """A paragraph of `sentences` sentences with `words_each` words each."""

    return " ".join(
        " ".join(f"{prefix}{n}x{i}" for i in range(words_each)).capitalize() + "."
        for n in range(sentences)
    )


def covered(chunks, blocks):
    return _coverage(chunks, blocks)


def assert_well_formed(chunks, blocks, maximum=480):
    """The rules every chunker must keep, whatever the strategy."""

    assert chunks, "no chunks"
    for chunk in chunks:
        assert chunk.text.strip()
        assert chunk.tokens <= maximum
        assert chunk.spans
        # never cuts inside a word: every span starts and ends on a word edge
        for block_index, start, end in chunk.spans:
            text = blocks[block_index].text
            assert 0 <= start < end <= len(text)
            assert start == 0 or text[start - 1].isspace() or text[start].isspace()
            assert end == len(text) or text[end].isspace() or text[end - 1].isspace()
    assert covered(chunks, blocks) >= 0.99
