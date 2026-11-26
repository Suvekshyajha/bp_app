import io
import time
import zipfile

import pytest

import limits
import parsers
from parsers import ParseError


# ---------- helpers ----------

def write(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return str(path)


def make_pdf(pages):
    """A tiny valid PDF. `pages` is a list of pages, each a list of text lines."""

    count = len(pages)
    page_ids = [4 + 2 * i for i in range(count)]
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, lines in enumerate(pages):
        content_id = 5 + 2 * i
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_id} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
            ).encode()
        )
        stream = "BT /F1 12 Tf 72 720 Td 14 TL\n"
        stream += "".join(f"({line}) Tj T*\n" for line in lines) + "ET"
        data = stream.encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def make_docx(tmp_path, build):
    from docx import Document

    document = Document()
    build(document)
    path = tmp_path / "file.docx"
    document.save(str(path))
    return str(path)


# ---------- txt ----------

def test_txt_gives_paragraphs_and_lists(tmp_path):
    path = write(tmp_path, "a.txt", "First paragraph.\n\n- item one\n- item two\n\nLast one.")

    parsed = parsers.parse_file(path, "txt")

    assert [b.type for b in parsed.blocks] == ["paragraph", "list", "paragraph"]
    assert parsed.blocks[1].text == "- item one\n- item two"
    assert parsed.source_type == "txt"
    assert parsed.page_count is None


def test_txt_with_a_byte_order_mark_and_windows_line_endings(tmp_path):
    path = write(tmp_path, "a.txt", b"\xef\xbb\xbfHello\r\n\r\nWorld")

    parsed = parsers.parse_file(path, "txt")

    assert [b.text for b in parsed.blocks] == ["Hello", "World"]


def test_txt_that_is_not_utf8_is_rejected(tmp_path):
    path = write(tmp_path, "a.txt", b"\xff\xfe\x00bad")

    with pytest.raises(ParseError) as error:
        parsers.parse_file(path, "txt")

    assert error.value.code == limits.ERR_UNSUPPORTED


def test_too_much_text_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "MAX_TEXT_CHARS", 20)
    path = write(tmp_path, "a.txt", "x" * 21)

    with pytest.raises(ParseError) as error:
        parsers.parse_file(path, "txt")

    assert error.value.code == limits.ERR_TOO_LARGE


# ---------- md ----------

MARKDOWN = """# Title

Intro line one
intro line two

## Part A

- first
- second
1. numbered

```python
x = 1

y = 2
```

| Name | Qty |
| --- | --- |
| apple | 3 |
| pear | 5 |

### Detail

Closing words.
"""


def test_md_gives_typed_blocks(tmp_path):
    parsed = parsers.parse_file(write(tmp_path, "a.md", MARKDOWN), "md")

    kinds = [(b.type, b.level) for b in parsed.blocks]

    assert kinds == [
        ("heading", 1),
        ("paragraph", None),
        ("heading", 2),
        ("list", None),
        ("code", None),
        ("table", None),
        ("heading", 3),
        ("paragraph", None),
    ]


def test_md_details(tmp_path):
    parsed = parsers.parse_file(write(tmp_path, "a.md", MARKDOWN), "md")
    by_type = {}
    for block in parsed.blocks:
        by_type.setdefault(block.type, []).append(block)

    assert by_type["paragraph"][0].text == "Intro line one\nintro line two"
    assert by_type["code"][0].text == "x = 1\n\ny = 2"
    assert by_type["table"][0].text == "Name | Qty\napple | 3\npear | 5"
    assert by_type["list"][0].text.splitlines() == ["- first", "- second", "1. numbered"]


def test_md_section_paths_follow_the_heading_stack(tmp_path):
    parsed = parsers.parse_file(write(tmp_path, "a.md", MARKDOWN), "md")

    paths = [block.section_path for block in parsed.blocks]

    assert paths[0] == ["Title"]  # a heading's own path includes itself
    assert paths[3] == ["Title", "Part A"]
    assert paths[6] == ["Title", "Part A", "Detail"]
    assert paths[7] == ["Title", "Part A", "Detail"]


def test_a_new_heading_at_the_same_level_replaces_the_old_one(tmp_path):
    path = write(tmp_path, "a.md", "# One\n\n## A\n\ntext\n\n## B\n\nmore\n\n# Two\n\nend")

    parsed = parsers.parse_file(path, "md")

    paths = [b.section_path for b in parsed.blocks if b.type == "paragraph"]
    assert paths == [["One", "A"], ["One", "B"], ["Two"]]


def test_text_before_any_heading_has_an_empty_path(tmp_path):
    parsed = parsers.parse_file(write(tmp_path, "a.md", "Hello\n\n# H\n\nBye"), "md")

    assert parsed.blocks[0].section_path == []


# ---------- docx ----------

def test_docx_gives_headings_lists_tables_in_order(tmp_path):
    def build(document):
        document.add_heading("Main title", level=1)
        document.add_paragraph("A normal paragraph.")
        document.add_heading("Sub part", level=2)
        document.add_paragraph("one", style="List Bullet")
        document.add_paragraph("two", style="List Bullet")
        table = document.add_table(rows=2, cols=2)
        table.rows[0].cells[0].text = "Name"
        table.rows[0].cells[1].text = "Qty"
        table.rows[1].cells[0].text = "apple"
        table.rows[1].cells[1].text = "3"
        document.add_paragraph("")  # empty paragraphs are skipped
        document.add_paragraph("The end.")

    parsed = parsers.parse_file(make_docx(tmp_path, build), "docx")

    assert [(b.type, b.level) for b in parsed.blocks] == [
        ("heading", 1),
        ("paragraph", None),
        ("heading", 2),
        ("list", None),
        ("table", None),
        ("paragraph", None),
    ]
    assert parsed.blocks[3].text == "one\ntwo"
    assert parsed.blocks[4].text == "Name | Qty\napple | 3"
    assert parsed.blocks[5].section_path == ["Main title", "Sub part"]


def test_docx_zip_bomb_is_rejected(tmp_path, monkeypatch):
    path = make_docx(tmp_path, lambda d: d.add_paragraph("hello"))
    monkeypatch.setattr(limits, "MAX_DOCX_UNCOMPRESSED_BYTES", 100)

    with pytest.raises(ParseError) as error:
        parsers.parse_file(path, "docx")

    assert error.value.code == limits.ERR_TOO_LARGE


def test_a_zip_that_is_not_a_docx_is_rejected(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes.txt", "hi")
    path = write(tmp_path, "a.docx", buffer.getvalue())

    with pytest.raises(ParseError) as error:
        parsers.parse_file(path, "docx")

    assert error.value.code == limits.ERR_UNSUPPORTED


# ---------- pdf ----------

def test_pdf_blocks_carry_page_numbers(tmp_path):
    path = write(tmp_path, "a.pdf", make_pdf([["First page text here"], ["Second page text"]]))

    parsed = parsers.parse_file(path, "pdf")

    assert parsed.page_count == 2
    assert [b.page for b in parsed.blocks] == [1, 2]
    assert "First page text" in parsed.blocks[0].text
    assert "Second page text" in parsed.blocks[1].text


def test_pdf_over_the_page_cap_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "MAX_PDF_PAGES", 1)
    path = write(tmp_path, "a.pdf", make_pdf([["one"], ["two"]]))

    with pytest.raises(ParseError) as error:
        parsers.parse_file(path, "pdf")

    assert error.value.code == limits.ERR_TOO_LARGE


def test_corrupt_pdf_fails_cleanly(tmp_path):
    path = write(tmp_path, "a.pdf", b"%PDF-1.4\nthis is not really a pdf at all")

    with pytest.raises(ParseError) as error:
        parsers.parse_file(path, "pdf")

    assert error.value.code == limits.ERR_UNSUPPORTED


def test_pdf_heading_heuristic():
    looks = parsers._looks_like_pdf_heading

    assert looks("INTRODUCTION")
    assert looks("Methods And Data")
    assert looks("Results of the Study")
    assert not looks("This ends with a period.")
    assert not looks("this starts lowercase")
    assert not looks("42")
    assert not looks("x" * 81)


def test_pdf_segment_makes_heading_only_when_alone():
    alone = parsers._pdf_segment_blocks(["OVERVIEW"], 3)
    with_text = parsers._pdf_segment_blocks(["OVERVIEW", "Some text follows here"], 3)

    assert [(b.type, b.level, b.page) for b in alone] == [("heading", 1, 3)]
    assert [b.type for b in with_text] == ["paragraph"]


def test_pdf_table_needs_three_aligned_lines_in_a_row():
    table_lines = ["Item    Qty    Price", "Apple   3      1.20", "Pear    5      0.80"]

    blocks = parsers._pdf_segment_blocks(table_lines, 1)
    short = parsers._pdf_segment_blocks(table_lines[:2], 1)

    assert [b.type for b in blocks] == ["table"]
    assert blocks[0].text.splitlines()[0] == "Item | Qty | Price"
    assert [b.type for b in short] == ["paragraph"]


# ---------- signatures (what the API checks before it accepts a file) ----------

def test_wrong_magic_bytes_are_rejected(tmp_path):
    fake_pdf = write(tmp_path, "evil.pdf", b"MZ\x90\x00 this is an exe")

    with pytest.raises(ParseError) as error:
        parsers.check_file_signature(fake_pdf, "pdf")

    assert error.value.code == limits.ERR_UNSUPPORTED


def test_empty_file_is_rejected_for_pdf_and_docx(tmp_path):
    for name, kind in (("e.pdf", "pdf"), ("e.docx", "docx")):
        with pytest.raises(ParseError):
            parsers.check_file_signature(write(tmp_path, name, b""), kind)


def test_source_type_comes_from_the_extension():
    assert parsers.source_type_from_filename("Report.PDF") == "pdf"
    assert parsers.source_type_from_filename("a.docx") == "docx"
    assert parsers.source_type_from_filename("notes.md") == "md"
    assert parsers.source_type_from_filename("a.txt") == "txt"
    assert parsers.source_type_from_filename("virus.exe") is None
    assert parsers.source_type_from_filename("noextension") is None


def test_unknown_source_type_is_rejected(tmp_path):
    with pytest.raises(ParseError):
        parsers.parse_file(write(tmp_path, "a.txt", "hi"), "exe")


# ---------- child process ----------

def _sleepy(connection, path, source_type):
    time.sleep(30)


def test_parse_in_child_returns_the_parsed_document(tmp_path):
    path = write(tmp_path, "a.txt", "Hello there.\n\nSecond.")

    parsed = parsers.parse_in_child(path, "txt", timeout=60)

    assert [b.text for b in parsed.blocks] == ["Hello there.", "Second."]


def test_parse_in_child_reports_a_safe_error_code(tmp_path):
    path = write(tmp_path, "a.pdf", b"not a pdf")

    with pytest.raises(ParseError) as error:
        parsers.parse_in_child(path, "pdf", timeout=60)

    assert error.value.code == limits.ERR_UNSUPPORTED


def test_a_slow_parser_is_killed_and_fails_with_timeout(tmp_path):
    path = write(tmp_path, "a.txt", "hi")
    started = time.monotonic()

    with pytest.raises(ParseError) as error:
        parsers.parse_in_child(path, "txt", timeout=1, target=_sleepy)

    assert error.value.code == limits.ERR_TIMEOUT
    assert time.monotonic() - started < 15
