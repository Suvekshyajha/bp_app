"""Check chunks after any chunker ran (plan 15.5)."""

import limits
from doc_model import Block, Chunk

COVERAGE_MINIMUM = 0.99
SMALL_CHUNK_SHARE = 0.05


class ChunkingError(Exception):
    """The document cannot be chunked. `code` is a safe error code (limits.py)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class TooManyChunks(ChunkingError):
    def __init__(self):
        super().__init__(limits.ERR_TOO_MANY_CHUNKS)


class ChunkingFailed(ChunkingError):
    def __init__(self):
        super().__init__(limits.ERR_UNSUPPORTED)


def _coverage(chunks: list[Chunk], blocks: list[Block]) -> float:
    """Share of the non-whitespace characters of the blocks that some chunk covers."""

    covered_ranges: dict[int, list[tuple[int, int]]] = {}
    for chunk in chunks:
        for block_index, start, end in chunk.spans:
            covered_ranges.setdefault(block_index, []).append((start, end))

    total = 0
    covered = 0
    for index, block in enumerate(blocks):
        text = block.text
        prefix = [0]
        for character in text:
            prefix.append(prefix[-1] + (0 if character.isspace() else 1))
        total += prefix[-1]

        merged: list[list[int]] = []
        for start, end in sorted(covered_ranges.get(index, [])):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        covered += sum(prefix[min(end, len(text))] - prefix[min(start, len(text))] for start, end in merged)

    return 1.0 if total == 0 else covered / total


def find_problem(chunks: list[Chunk], blocks: list[Block], params: dict) -> str | None:
    """None when the chunks are fine, otherwise the name of the rule that failed.

    Raises TooManyChunks, because a document that is simply too big gets no fallback.
    """

    if len(chunks) > limits.MAX_CHUNKS_PER_DOCUMENT:
        raise TooManyChunks()

    if not chunks:
        return "no_chunks"

    for chunk in chunks:
        if not chunk.text.strip():
            return "empty_chunk"
        if chunk.tokens > limits.HARD_MAX_CHUNK_TOKENS:
            return "oversize_chunk"

    # The last chunk of a section may be small, so it is not counted
    small = 0
    for position, chunk in enumerate(chunks):
        is_last_of_section = (
            position == len(chunks) - 1
            or chunks[position + 1].section_title != chunk.section_title
        )
        if chunk.tokens < params["min_chunk_size"] and not is_last_of_section:
            small += 1
    if small / len(chunks) > SMALL_CHUNK_SHARE:
        return "too_many_small"

    if _coverage(chunks, blocks) < COVERAGE_MINIMUM:
        return "low_coverage"

    return None
