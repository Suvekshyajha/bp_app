from chunk_helpers import assert_well_formed, make_doc, params, prose, words
from chunking.hybrid import hybrid
from doc_model import Block


def table(rows):
    return Block("table", "\n".join(["Name | Qty | Note"] + [f"item{i} | {i} | {words(5, f'n{i}_')}" for i in range(rows)]))


def test_tables_and_code_are_never_merged_with_prose():
    doc = make_doc([
        Block("heading", "Intro", level=1),
        Block("paragraph", prose(4, 10)),
        table(3),
        Block("code", "print(1)\nprint(2)"),
        Block("paragraph", prose(4, 10, "after")),
    ])

    chunks = hybrid(doc.blocks, params())

    assert_well_formed(chunks, doc.blocks)
    kinds = []
    for chunk in chunks:
        has_table = "Name | Qty" in chunk.text
        has_code = "print(1)" in chunk.text
        has_prose = "x0" in chunk.text
        assert [has_table, has_code, has_prose].count(True) == 1
        kinds.append("table" if has_table else "code" if has_code else "prose")
    assert kinds == ["prose", "table", "code", "prose"]


def test_a_small_table_is_one_chunk():
    doc = make_doc([table(5)])

    chunks = hybrid(doc.blocks, params(table_chunk_size=400))

    assert len(chunks) == 1
    assert chunks[0].text == doc.blocks[0].text


def test_a_big_table_is_split_by_rows_with_the_header_repeated():
    doc = make_doc([table(80)])

    chunks = hybrid(doc.blocks, params(table_chunk_size=100))

    assert len(chunks) > 3
    assert_well_formed(chunks, doc.blocks)
    for chunk in chunks:
        assert chunk.text.splitlines()[0] == "Name | Qty | Note"
        assert chunk.tokens <= 120
    # every data row is in exactly one chunk
    for number in range(80):
        assert sum(f"item{number} |" in c.text for c in chunks) == 1


def test_a_big_code_block_is_split_between_lines_with_fences():
    lines = [f"line_{i} = compute({i}, {i + 1}, {i + 2})" for i in range(200)]
    doc = make_doc([Block("code", "\n".join(lines))])

    chunks = hybrid(doc.blocks, params(code_chunk_size=100))

    assert len(chunks) > 3
    assert_well_formed(chunks, doc.blocks)
    for chunk in chunks:
        assert chunk.text.startswith("```\n") and chunk.text.endswith("\n```")
        for line in chunk.text.splitlines()[1:-1]:
            assert line.startswith("line_") and line.endswith(")")


def test_prose_uses_heading_aware_when_the_document_has_headings():
    doc = make_doc([
        Block("heading", "One", level=1), Block("paragraph", words(90, "a")),
        table(2),
        Block("heading", "Two", level=1), Block("paragraph", words(90, "b")),
    ])

    chunks = hybrid(doc.blocks, params(chunk_overlap=0))

    titles = [c.section_title for c in chunks]
    assert titles[0] == "One" and titles[-1] == "Two"


def test_prose_uses_recursive_when_there_are_no_headings():
    doc = make_doc([Block("paragraph", words(900)), table(2)])

    chunks = hybrid(doc.blocks, params())

    assert_well_formed(chunks, doc.blocks)
    assert sum("Name | Qty" in c.text for c in chunks) == 1


def test_table_chunks_carry_the_section_title_and_page():
    blocks = [Block("heading", "Data", level=1, page=2), Block("table", table(2).text, page=2)]
    doc = make_doc(blocks)

    chunks = hybrid(doc.blocks, params())
    found = [c for c in chunks if "Name | Qty" in c.text][0]

    assert found.section_title == "Data"
    assert found.page == 2
