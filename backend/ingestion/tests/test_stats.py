from doc_model import Block, ParsedDocument
from stats import compute_stats


def _doc(blocks, source_type="md", pages=None):
    return ParsedDocument(blocks=blocks, source_type=source_type, page_count=pages)


def test_counts_blocks_by_type():
    parsed = _doc(
        [
            Block("heading", "Intro", level=1),
            Block("paragraph", "one two three"),
            Block("paragraph", "four five"),
            Block("list", "- a\n- b"),
            Block("table", "a | b\n1 | 2"),
            Block("code", "print(1)"),
        ]
    )

    stats = compute_stats(parsed)

    assert stats["blocks"] == {
        "heading": 1, "paragraph": 2, "list": 1, "table": 1, "code": 1,
    }


def test_paragraph_sizes_use_the_token_counter():
    parsed = _doc([Block("paragraph", "a b c d"), Block("paragraph", "a b")])

    stats = compute_stats(parsed)

    assert stats["avg_paragraph_tokens"] == 3
    assert stats["max_paragraph_tokens"] == 4
    assert stats["tokens"] == 6


def test_chars_pages_and_source_type():
    parsed = _doc([Block("paragraph", "hello")], source_type="pdf", pages=7)

    stats = compute_stats(parsed)

    assert stats["chars"] == 5
    assert stats["pages"] == 7
    assert stats["source_type"] == "pdf"


def test_list_ratio_is_lists_over_all_blocks():
    parsed = _doc(
        [Block("list", "- a"), Block("paragraph", "x"), Block("paragraph", "y"), Block("paragraph", "z")]
    )

    assert compute_stats(parsed)["list_ratio"] == 0.25


def test_heading_depth_and_hierarchy():
    parsed = _doc(
        [
            Block("heading", "A", level=1),
            Block("heading", "B", level=2),
            Block("heading", "C", level=2),
        ]
    )

    stats = compute_stats(parsed)

    assert stats["heading_depth"] == 2
    assert stats["has_hierarchy"] is True


def test_two_headings_are_not_a_hierarchy():
    parsed = _doc([Block("heading", "A", level=1), Block("heading", "B", level=2)])

    assert compute_stats(parsed)["has_hierarchy"] is False


def test_empty_document_gives_zeros():
    stats = compute_stats(_doc([]))

    assert stats["tokens"] == 0
    assert stats["list_ratio"] == 0.0
    assert stats["heading_depth"] == 0
    assert stats["avg_paragraph_tokens"] == 0
    assert stats["has_hierarchy"] is False


def test_same_input_gives_same_stats():
    blocks = [Block("heading", "A", level=1), Block("paragraph", "one two")]

    assert compute_stats(_doc(blocks)) == compute_stats(_doc(blocks))
