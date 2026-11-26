"""Plain data classes shared by the parser, the stats and the chunkers.

They are plain dataclasses on purpose: the parser runs in a child process and
sends its result back, so everything here must be picklable.
"""

from dataclasses import dataclass, field

# Longest heading kept in a section path (one item).
MAX_PATH_ITEM_CHARS = 120


@dataclass
class Block:
    type: str  # heading | paragraph | list | table | code
    text: str  # table = rows joined by newline, cells by " | ", header first
    page: int | None = None  # 1-based, PDF only
    level: int | None = None  # heading level 1..6
    section_path: list[str] = field(default_factory=list)  # filled after parsing


@dataclass
class ParsedDocument:
    blocks: list[Block]
    source_type: str  # pdf | docx | txt | md
    page_count: int | None


@dataclass
class Chunk:
    index: int
    text: str
    tokens: int
    section_title: str | None  # " > ".join(last two path items), at most 120 chars
    page: int | None  # first page the chunk touches
    spans: list[tuple[int, int, int]]  # (block_index, start_char, end_char)


def assign_section_paths(blocks: list[Block]) -> None:
    """Fill `section_path` on every block from a stack of headings.

    A heading at level N removes every heading of level N or deeper from the
    stack, then adds itself. A heading's own path includes itself. Blocks
    before the first heading get an empty path.
    """

    stack: list[tuple[int, str]] = []

    for block in blocks:
        if block.type == "heading":
            level = block.level or 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, block.text.strip()[:MAX_PATH_ITEM_CHARS]))

        block.section_path = [title for _, title in stack]
