"""Turn an uploaded file into typed blocks (heading, paragraph, list, table, code).

Rules for each file type are in the migration plan, section 15.1. The parser
never claims a structure the text does not show: a PDF has no real headings,
so only a clear short title-like line on its own becomes one.

Two entry points:
- parse_file()      does the work. It must run in a child process.
- parse_in_child()  runs parse_file() in a child process with a timeout.

The libraries (pypdf, python-docx) are imported inside the functions that need
them, so the API process can use check_file_signature() without loading them.
Any library error is turned into a short safe code, never a stack trace.
"""

import multiprocessing
import os
import re
import zipfile

import limits
from doc_model import Block, ParsedDocument, assign_section_paths


class ParseError(Exception):
    """The file cannot be used. `code` is one of the safe codes in limits.py."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_EXTENSION_TO_TYPE = {".pdf": "pdf", ".docx": "docx", ".txt": "txt", ".md": "md"}


def source_type_from_filename(filename: str) -> str | None:
    extension = os.path.splitext(filename.lower())[1]
    return _EXTENSION_TO_TYPE.get(extension)


# ---------- Checks that do not need a parsing library ----------

def _read_text(path: str) -> str:
    with open(path, "rb") as file:
        raw = file.read()

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ParseError(limits.ERR_UNSUPPORTED) from None

    if len(text) > limits.MAX_TEXT_CHARS:
        raise ParseError(limits.ERR_TOO_LARGE)

    return text.replace("\r\n", "\n").replace("\r", "\n")


def check_file_signature(path: str, source_type: str) -> None:
    """Check the real content, not just the extension (a .exe renamed to .pdf).

    PDF starts with %PDF-. DOCX is a zip that holds word/document.xml. Text
    must decode as UTF-8 after the BOM is removed.
    """

    try:
        if source_type == "pdf":
            with open(path, "rb") as file:
                if file.read(5) != b"%PDF-":
                    raise ParseError(limits.ERR_UNSUPPORTED)

        elif source_type == "docx":
            if not zipfile.is_zipfile(path):
                raise ParseError(limits.ERR_UNSUPPORTED)
            with zipfile.ZipFile(path) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise ParseError(limits.ERR_UNSUPPORTED)

        elif source_type in ("txt", "md"):
            _read_text(path)

        else:
            raise ParseError(limits.ERR_UNSUPPORTED)

    except ParseError:
        raise
    except Exception:
        raise ParseError(limits.ERR_UNSUPPORTED) from None


# ---------- Shared helpers ----------

def _split_segments(lines: list[str]) -> list[list[str]]:
    """Group lines into runs separated by blank lines."""

    segments: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if line.strip():
            current.append(line.rstrip())
        elif current:
            segments.append(current)
            current = []

    if current:
        segments.append(current)

    return segments


_PLAIN_LIST_LINE = re.compile(r"^\s*[-*]\s+\S")


def _paragraphs_and_lists(lines: list[str], page: int | None = None) -> list[Block]:
    """Lines with no blank line inside: runs of list lines and runs of others."""

    blocks: list[Block] = []
    run: list[str] = []
    run_is_list = False

    def flush() -> None:
        if run:
            kind = "list" if run_is_list else "paragraph"
            blocks.append(Block(kind, "\n".join(s.strip() for s in run), page=page))
            run.clear()

    for line in lines:
        is_list = bool(_PLAIN_LIST_LINE.match(line))
        if run and is_list != run_is_list:
            flush()
        run_is_list = is_list
        run.append(line)

    flush()
    return blocks


# ---------- txt ----------

def _parse_txt(path: str) -> tuple[list[Block], int | None]:
    blocks: list[Block] = []

    for segment in _split_segments(_read_text(path).split("\n")):
        blocks.extend(_paragraphs_and_lists(segment))

    return blocks, None


# ---------- md ----------

_MD_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
_MD_FENCE = re.compile(r"^\s*(```|~~~)")
_MD_LIST = re.compile(r"^\s*(?:[-*]|\d+\.)\s+\S")
_MD_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def _md_row(line: str) -> str:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return " | ".join(cells)


def _parse_md(path: str) -> tuple[list[Block], int | None]:
    lines = _read_text(path).split("\n")
    blocks: list[Block] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(Block("paragraph", "\n".join(paragraph)))
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]

        if not line.strip():
            flush_paragraph()
            i += 1
            continue

        fence = _MD_FENCE.match(line)
        if fence:
            flush_paragraph()
            marker = fence.group(1)
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith(marker):
                code.append(lines[i])
                i += 1
            i += 1  # the closing fence (or the end of the file)
            text = "\n".join(code).strip("\n")
            if text.strip():
                blocks.append(Block("code", text))
            continue

        heading = _MD_HEADING.match(line)
        if heading:
            flush_paragraph()
            if heading.group(2).strip():
                blocks.append(
                    Block("heading", heading.group(2).strip(), level=len(heading.group(1)))
                )
            i += 1
            continue

        is_table = (
            "|" in line
            and i + 1 < len(lines)
            and "|" in lines[i + 1]
            and bool(_MD_TABLE_SEPARATOR.match(lines[i + 1]))
        )
        if is_table:
            flush_paragraph()
            rows = [_md_row(line)]
            i += 2  # the header and the ---|--- line
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                rows.append(_md_row(lines[i]))
                i += 1
            blocks.append(Block("table", "\n".join(rows)))
            continue

        if _MD_LIST.match(line):
            flush_paragraph()
            items: list[str] = []
            while i < len(lines) and lines[i].strip() and (
                _MD_LIST.match(lines[i]) or lines[i][:1].isspace()
            ):
                items.append(lines[i].strip())
                i += 1
            blocks.append(Block("list", "\n".join(items)))
            continue

        paragraph.append(line.strip())
        i += 1

    flush_paragraph()
    return blocks, None


# ---------- docx ----------

_HEADING_STYLE = re.compile(r"^heading\s+(\d)$", re.IGNORECASE)


def _docx_heading_level(style_name: str) -> int | None:
    if style_name.strip().lower() == "title":
        return 1

    match = _HEADING_STYLE.match(style_name.strip())
    if match:
        return min(max(int(match.group(1)), 1), 6)

    return None


def _parse_docx(path: str) -> tuple[list[Block], int | None]:
    # Zip-bomb guard: check the unpacked size BEFORE python-docx reads it
    with zipfile.ZipFile(path) as archive:
        unpacked = sum(info.file_size for info in archive.infolist())
    if unpacked > limits.MAX_DOCX_UNCOMPRESSED_BYTES:
        raise ParseError(limits.ERR_TOO_LARGE)

    from docx import Document as DocxDocument
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = DocxDocument(path)
    blocks: list[Block] = []
    list_items: list[str] = []

    def flush_list() -> None:
        if list_items:
            blocks.append(Block("list", "\n".join(list_items)))
            list_items.clear()

    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]

        if tag == "p":
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue

            try:
                style_name = paragraph.style.name or ""
            except Exception:
                style_name = ""

            level = _docx_heading_level(style_name)
            if level:
                flush_list()
                blocks.append(Block("heading", text, level=level))
            elif style_name.lower().startswith("list"):
                list_items.append(text)
            else:
                flush_list()
                blocks.append(Block("paragraph", text))

        elif tag == "tbl":
            flush_list()
            rows: list[str] = []
            for row in Table(child, document).rows:
                cells: list[str] = []
                previous = None
                for cell in row.cells:
                    if cell._tc is previous:  # a merged cell shows up twice
                        continue
                    previous = cell._tc
                    cells.append(cell.text.strip().replace("\n", " "))
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                blocks.append(Block("table", "\n".join(rows)))

    flush_list()
    return blocks, None


# ---------- pdf ----------

_SMALL_WORDS = {"a", "an", "the", "of", "in", "on", "and", "or", "for", "to", "at", "by", "with", "vs"}


def _looks_like_pdf_heading(line: str) -> bool:
    """A short line, ALL CAPS or Title Case, with no final period."""

    text = line.strip()
    if not text or len(text) > 80 or text.endswith("."):
        return False
    if not any(character.isalpha() for character in text):
        return False
    if text.isupper():
        return True

    words = text.split()
    if len(words) > 12 or not words[0][:1].isupper():
        return False

    for word in words:
        first = word[:1]
        if first.isalpha() and not first.isupper() and word.lower() not in _SMALL_WORDS:
            return False

    return True


_TABLE_GAP = re.compile(r"\t| {2,}")


def _is_table_line(line: str) -> bool:
    """Several runs of two or more spaces (or tabs) inside the line."""

    return len(_TABLE_GAP.findall(line.strip())) >= 2


def _pdf_segment_blocks(lines: list[str], page: int) -> list[Block]:
    if len(lines) == 1 and _looks_like_pdf_heading(lines[0]):
        return [Block("heading", lines[0].strip(), page=page, level=1)]

    blocks: list[Block] = []
    text_lines: list[str] = []
    table_lines: list[str] = []

    def flush_text() -> None:
        if text_lines:
            blocks.append(
                Block("paragraph", "\n".join(s.strip() for s in text_lines), page=page)
            )
            text_lines.clear()

    def flush_table() -> None:
        # A table needs 3 or more lines in a row, otherwise it is plain text
        if len(table_lines) >= 3:
            flush_text()
            rows = [" | ".join(_TABLE_GAP.split(row.strip())) for row in table_lines]
            blocks.append(Block("table", "\n".join(rows), page=page))
        else:
            text_lines.extend(table_lines)
        table_lines.clear()

    for line in lines:
        if _is_table_line(line):
            table_lines.append(line)
        else:
            flush_table()
            text_lines.append(line)

    flush_table()
    flush_text()
    return blocks


def _parse_pdf(path: str) -> tuple[list[Block], int | None]:
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ParseError(limits.ERR_UNSUPPORTED)

    page_count = len(reader.pages)
    if page_count > limits.MAX_PDF_PAGES:
        raise ParseError(limits.ERR_TOO_LARGE)

    blocks: list[Block] = []
    total_chars = 0

    for number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").replace("\r\n", "\n").replace("\r", "\n")

        total_chars += len(text)
        if total_chars > limits.MAX_TEXT_CHARS:
            raise ParseError(limits.ERR_TOO_LARGE)

        for segment in _split_segments(text.split("\n")):
            blocks.extend(_pdf_segment_blocks(segment, number))

    return blocks, page_count


# ---------- Entry points ----------

_PARSERS = {
    "txt": _parse_txt,
    "md": _parse_md,
    "docx": _parse_docx,
    "pdf": _parse_pdf,
}


def parse_file(path: str, source_type: str) -> ParsedDocument:
    """Parse one file. Raises ParseError with a safe code, nothing else."""

    if source_type not in _PARSERS:
        raise ParseError(limits.ERR_UNSUPPORTED)

    check_file_signature(path, source_type)

    try:
        blocks, page_count = _PARSERS[source_type](path)
    except ParseError:
        raise
    except Exception:
        # Library messages can hold file content, so none of it is passed on
        raise ParseError(limits.ERR_UNSUPPORTED) from None

    if sum(len(block.text) for block in blocks) > limits.MAX_TEXT_CHARS:
        raise ParseError(limits.ERR_TOO_LARGE)

    assign_section_paths(blocks)
    return ParsedDocument(blocks=blocks, source_type=source_type, page_count=page_count)


def _child_main(connection, path: str, source_type: str) -> None:
    try:
        connection.send(("ok", parse_file(path, source_type)))
    except ParseError as error:
        connection.send(("error", error.code))
    except BaseException:
        connection.send(("error", limits.ERR_UNSUPPORTED))
    finally:
        connection.close()


def parse_in_child(path: str, source_type: str, timeout: float | None = None, target=_child_main) -> ParsedDocument:
    """Run parse_file() in its own process and kill it if it takes too long.

    A broken or hostile file can hang or crash a parser. In a child process
    that costs one process, not the worker. `target` exists only so tests can
    swap in a slow function.
    """

    wait = limits.PARSE_TIMEOUT_SECONDS if timeout is None else timeout

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=target, args=(sender, path, source_type), daemon=True)
    process.start()
    sender.close()

    try:
        if not receiver.poll(wait):
            raise ParseError(limits.ERR_TIMEOUT)

        try:
            kind, value = receiver.recv()
        except (EOFError, OSError):
            # The child died without answering (a crash)
            raise ParseError(limits.ERR_UNSUPPORTED) from None

        if kind == "ok":
            return value

        raise ParseError(value)

    finally:
        receiver.close()
        if process.is_alive():
            process.kill()
        process.join(5)
