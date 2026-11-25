"""hybrid: prose, tables and code are chunked separately (plan 15.4).

Tables and code are never merged with prose. Prose runs go to heading_aware
when the document has headings, otherwise to recursive.
"""

from chunking.strategies import Meter, TextView, heading_aware, make_chunks, recursive, section_title
from chunking.tokens import count_tokens
from doc_model import Block, Chunk

PROSE = ("heading", "paragraph", "list")
FENCE = "```"


def _line_offsets(text: str) -> list[tuple[int, int]]:
    offsets = []
    position = 0
    for line in text.split("\n"):
        offsets.append((position, position + len(line)))
        position += len(line) + 1
    return offsets


def _chunk_for(block: Block, block_index: int, text: str, spans: list[tuple[int, int, int]]) -> Chunk:
    return Chunk(
        index=0,
        text=text,
        tokens=count_tokens(text),
        section_title=section_title(block.section_path),
        page=block.page,
        spans=spans,
    )


def _table_chunks(block: Block, block_index: int, limit: int, meter: Meter) -> list[Chunk]:
    """One chunk if it fits, else groups of rows with the header row repeated."""

    if meter.text(block.text) <= limit:
        return [_chunk_for(block, block_index, block.text, [(block_index, 0, len(block.text))])]

    lines = block.text.split("\n")
    offsets = _line_offsets(block.text)
    header, header_span = lines[0], (block_index, offsets[0][0], offsets[0][1])
    header_tokens = meter.text(header)

    chunks: list[Chunk] = []
    rows: list[int] = []
    used = header_tokens

    def flush() -> None:
        if rows:
            text = "\n".join([header] + [lines[i] for i in rows])
            spans = [header_span] + [(block_index, offsets[i][0], offsets[i][1]) for i in rows]
            chunks.append(_chunk_for(block, block_index, text, spans))
            rows.clear()

    for number in range(1, len(lines)):
        size = meter.text(lines[number])
        if rows and used + size > limit:
            flush()
            used = header_tokens
        rows.append(number)
        used += size
    flush()

    return chunks or [_chunk_for(block, block_index, block.text, [(block_index, 0, len(block.text))])]


def _code_chunks(block: Block, block_index: int, limit: int, meter: Meter) -> list[Chunk]:
    """One chunk if it fits, else pieces cut between lines, each with fence lines."""

    if meter.text(block.text) <= limit:
        return [_chunk_for(block, block_index, block.text, [(block_index, 0, len(block.text))])]

    lines = block.text.split("\n")
    offsets = _line_offsets(block.text)
    chunks: list[Chunk] = []
    piece: list[int] = []
    used = 0

    def flush() -> None:
        if piece:
            body = "\n".join(lines[i] for i in piece)
            span = (block_index, offsets[piece[0]][0], offsets[piece[-1]][1])
            chunks.append(_chunk_for(block, block_index, f"{FENCE}\n{body}\n{FENCE}", [span]))
            piece.clear()

    for number, line in enumerate(lines):
        size = meter.text(line)
        if piece and used + size > limit:
            flush()
            used = 0
        piece.append(number)
        used += size
    flush()

    return chunks


def hybrid(blocks: list[Block], params: dict, indices: list[int] | None = None) -> list[Chunk]:
    order = list(range(len(blocks))) if indices is None else list(indices)
    meter = Meter()
    has_headings = any(blocks[i].type == "heading" for i in order)
    prose_strategy = heading_aware if has_headings else recursive

    chunks: list[Chunk] = []
    prose: list[int] = []

    def flush_prose() -> None:
        if prose:
            chunks.extend(prose_strategy(blocks, params, list(prose)))
            prose.clear()

    for block_index in order:
        block = blocks[block_index]
        if block.type == "table":
            flush_prose()
            chunks.extend(_table_chunks(block, block_index, params["table_chunk_size"], meter))
        elif block.type == "code":
            flush_prose()
            chunks.extend(_code_chunks(block, block_index, params["code_chunk_size"], meter))
        else:
            prose.append(block_index)

    flush_prose()
    return chunks
