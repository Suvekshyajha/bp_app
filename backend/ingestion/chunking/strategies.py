"""Stage 2: the deterministic chunkers (plan 15.4). No LLM, no randomness.

Every chunker takes the document blocks and the clamped parameters and returns
chunks. They all work on a TextView: the blocks joined with blank lines into
one string, plus a map from positions in that string back to the blocks. A
chunk is just a range of the string, so:
- it never cuts inside a word (ranges start and end at word edges),
- it knows exactly which source blocks it covers (`spans`),
- overlap is simply two ranges that share some text.

Sizes are tokens. To keep packing fast, tokens are counted per word (cached);
for BERT-style tokenizers that gives the same total as counting the whole text.
The final `Chunk.tokens` is counted on the real text.
"""

import re
from bisect import bisect_right

import limits
from chunking.tokens import count_tokens
from doc_model import Block, Chunk

JOINER = "\n\n"
MAX_TITLE_CHARS = 120


class ChunkingFallback(Exception):
    """This strategy cannot be used for this document. `reason` says why.
    run_chunking() then uses the recursive chunker instead."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------- Counting ----------

class Meter:
    """Token counts, cached per word."""

    def __init__(self):
        self._words: dict[str, int] = {}

    def word(self, word: str) -> int:
        cached = self._words.get(word)
        if cached is None:
            cached = count_tokens(word)
            self._words[word] = cached
        return cached

    def text(self, text: str) -> int:
        return sum(self.word(word) for word in text.split())


# ---------- The joined text and its map back to the blocks ----------

class TextView:
    def __init__(self, blocks: list[Block], indices: list[int] | None = None):
        self.blocks = blocks
        self.indices = list(range(len(blocks))) if indices is None else list(indices)

        parts: list[str] = []
        self.segments: list[tuple[int, int, int]] = []  # (start, end, block index)
        position = 0
        for number, block_index in enumerate(self.indices):
            if number:
                position += len(JOINER)
            text = blocks[block_index].text
            self.segments.append((position, position + len(text), block_index))
            parts.append(text)
            position += len(text)

        self.text = JOINER.join(parts)
        self._starts = [segment[0] for segment in self.segments]

    def spans_for(self, start: int, end: int) -> list[tuple[int, int, int]]:
        """(block index, start in block, end in block) for every block touched."""

        spans: list[tuple[int, int, int]] = []
        first = max(0, bisect_right(self._starts, start) - 1)
        for seg_start, seg_end, block_index in self.segments[first:]:
            if seg_start >= end:
                break
            low, high = max(start, seg_start), min(end, seg_end)
            if low < high:
                spans.append((block_index, low - seg_start, high - seg_start))
        return spans


def section_title(path: list[str]) -> str | None:
    title = " > ".join(path[-2:])[:MAX_TITLE_CHARS]
    return title or None


def make_chunks(view: TextView, ranges: list[tuple[int, int]]) -> list[Chunk]:
    """Turn ranges of the view's text into chunks. Empty ranges are dropped."""

    chunks: list[Chunk] = []
    for start, end in ranges:
        while start < end and view.text[start].isspace():
            start += 1
        while end > start and view.text[end - 1].isspace():
            end -= 1
        if start >= end:
            continue

        spans = view.spans_for(start, end)
        if not spans:
            continue

        text = view.text[start:end]
        first = view.blocks[spans[0][0]]
        chunks.append(
            Chunk(
                index=0,  # numbered by run_chunking
                text=text,
                tokens=count_tokens(text),
                section_title=section_title(first.section_path),
                page=first.page,
                spans=spans,
            )
        )
    return chunks


# ---------- Splitting helpers ----------

_ABBREVIATIONS = {
    "e.g.", "i.e.", "dr.", "mr.", "mrs.", "ms.", "prof.", "fig.", "vs.", "etc.",
    "no.", "eq.", "sec.", "al.", "approx.", "inc.", "ltd.", "st.", "jr.", "sr.",
}

# a sentence end, a paragraph break, or a new list item
_BOUNDARY = re.compile(
    r"(?P<stop>[.!?][\"')\]]*)(?P<gap>[ \t]+)(?=[A-Z0-9\"'(\[\u201c\u2018])"
    r"|(?P<para>\n[ \t]*\n\s*)"
    r"|(?P<item>\n(?=[ \t]*(?:[-*]|\d+\.)[ \t]))"
)


def sentence_ranges(text: str, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
    """Sentence ranges inside text[start:end]. The gaps between them are only
    whitespace, so nothing is lost."""

    end = len(text) if end is None else end
    ranges: list[tuple[int, int]] = []
    position = start

    for match in _BOUNDARY.finditer(text, start, end):
        if match.group("stop"):
            before = text[position : match.start() + 1].split()
            if before and before[-1].lower() in _ABBREVIATIONS:
                continue
            sentence_end = match.start() + len(match.group("stop"))
        else:
            sentence_end = match.start()

        if sentence_end > position:
            ranges.append((position, sentence_end))
        position = match.end()

    if position < end:
        ranges.append((position, end))

    return [(a, b) for a, b in ranges if text[a:b].strip()]


_SEPARATORS = ("\n\n", "\n", ". ", " ")


def _cut_by_characters(text: str, start: int, end: int, limit: int, meter: Meter) -> list[tuple[int, int]]:
    """Last resort for a single "word" longer than the limit (for example a
    long URL). The only place a word can be cut."""

    pieces: list[tuple[int, int]] = []
    position = start
    while position < end:
        stop = min(end, position + max(1, limit * 4))
        while stop > position + 1 and meter.text(text[position:stop]) > limit:
            stop = position + max(1, (stop - position) // 2)
        pieces.append((position, stop))
        position = stop
    return pieces


def leaf_ranges(text: str, start: int, end: int, limit: int, meter: Meter, level: int = 0) -> list[tuple[int, int]]:
    """Split text[start:end] into pieces of at most `limit` tokens, using the
    separators in order: blank line, newline, sentence end, space."""

    if meter.text(text[start:end]) <= limit:
        return [(start, end)]

    if level >= len(_SEPARATORS):
        return _cut_by_characters(text, start, end, limit, meter)

    separator = _SEPARATORS[level]
    pieces: list[tuple[int, int]] = []
    position = start
    while True:
        found = text.find(separator, position, end)
        if found == -1:
            pieces.append((position, end))
            break
        keep = 1 if separator == ". " else 0  # the full stop stays with its sentence
        pieces.append((position, found + keep))
        position = found + len(separator)

    if len(pieces) == 1:
        return leaf_ranges(text, start, end, limit, meter, level + 1)

    leaves: list[tuple[int, int]] = []
    for piece_start, piece_end in pieces:
        if text[piece_start:piece_end].strip():
            leaves.extend(leaf_ranges(text, piece_start, piece_end, limit, meter, level + 1))
    return leaves


def pack(units: list[tuple[int, int, int]], size: int, overlap: int) -> list[tuple[int, int]]:
    """Group consecutive units (start, end, tokens) into ranges of up to `size`
    tokens. The next range starts with the trailing units worth at most
    `overlap` tokens of the previous one."""

    ranges: list[tuple[int, int]] = []
    count = len(units)
    first = 0

    while first < count:
        last = first
        total = 0
        while last < count and (last == first or total + units[last][2] <= size):
            total += units[last][2]
            last += 1

        ranges.append((units[first][0], units[last - 1][1]))
        if last >= count:
            break

        back = last
        carried = 0
        while back > first + 1 and carried + units[back - 1][2] <= overlap:
            carried += units[back - 1][2]
            back -= 1
        first = back

    return ranges


def absorb_small_tail(ranges: list[tuple[int, int]], text: str, meter: Meter, minimum: int, maximum: int) -> list[tuple[int, int]]:
    """A last piece below the minimum joins the one before it, if that still fits."""

    if len(ranges) >= 2:
        (a1, _), (_, b2) = ranges[-2], ranges[-1]
        last_start, last_end = ranges[-1]
        if meter.text(text[last_start:last_end]) < minimum and meter.text(text[a1:b2]) <= maximum:
            return ranges[:-2] + [(a1, b2)]
    return ranges


def _units(text: str, ranges: list[tuple[int, int]], meter: Meter) -> list[tuple[int, int, int]]:
    return [(a, b, meter.text(text[a:b])) for a, b in ranges]


def sentence_units(text: str, start: int, end: int, meter: Meter, maximum: int) -> list[tuple[int, int, int]]:
    """Sentences as (start, end, tokens). One longer than `maximum` is split
    with the recursive separators."""

    units: list[tuple[int, int, int]] = []
    for a, b in sentence_ranges(text, start, end):
        tokens = meter.text(text[a:b])
        if tokens > maximum:
            units.extend(_units(text, leaf_ranges(text, a, b, maximum, meter), meter))
        else:
            units.append((a, b, tokens))
    return units


# ---------- The strategies ----------

def single_chunk(blocks: list[Block], params: dict, indices: list[int] | None = None) -> list[Chunk]:
    view = TextView(blocks, indices)
    if Meter().text(view.text) > limits.SINGLE_CHUNK_MAX_TOKENS:
        raise ChunkingFallback("too_long_for_single")
    return make_chunks(view, [(0, len(view.text))])


def fixed_size(blocks: list[Block], params: dict, indices: list[int] | None = None) -> list[Chunk]:
    """Greedy packing of words. Ignores structure on purpose."""

    view = TextView(blocks, indices)
    meter = Meter()
    size, overlap = params["chunk_size"], params["chunk_overlap"]

    words = [(m.start(), m.end()) for m in re.finditer(r"\S+", view.text)]
    weights = [meter.word(view.text[a:b]) for a, b in words]

    ranges: list[tuple[int, int]] = []
    first = 0
    while first < len(words):
        last = first
        total = 0
        while last < len(words) and (last == first or total + weights[last] <= size):
            total += weights[last]
            last += 1
        ranges.append((words[first][0], words[last - 1][1]))
        if last >= len(words):
            break

        back = last
        carried = 0
        while back > first + 1 and carried + weights[back - 1] <= overlap:
            carried += weights[back - 1]
            back -= 1
        first = back

    ranges = absorb_small_tail(ranges, view.text, meter, params["min_chunk_size"], params["max_chunk_size"])
    return make_chunks(view, ranges)


def sentence(blocks: list[Block], params: dict, indices: list[int] | None = None, min_overlap: int = 0) -> list[Chunk]:
    """Whole sentences up to the size; the overlap is trailing sentences."""

    view = TextView(blocks, indices)
    meter = Meter()
    units = sentence_units(view.text, 0, len(view.text), meter, params["max_chunk_size"])
    overlap = max(params["chunk_overlap"], min_overlap)

    ranges = pack(units, params["chunk_size"], overlap)
    ranges = absorb_small_tail(ranges, view.text, meter, params["min_chunk_size"], params["max_chunk_size"])
    return make_chunks(view, ranges)


def sliding_window(blocks: list[Block], params: dict, indices: list[int] | None = None) -> list[Chunk]:
    """Sentence windows where every window overlaps its neighbours (at least 20%)."""

    return sentence(blocks, params, indices, min_overlap=int(params["chunk_size"] * 0.2))


def paragraph(blocks: list[Block], params: dict, indices: list[int] | None = None) -> list[Chunk]:
    """Each block is a unit. A block that is too big is split by sentences, and
    only those pieces overlap."""

    view = TextView(blocks, indices)
    meter = Meter()
    size, overlap, maximum = params["chunk_size"], params["chunk_overlap"], params["max_chunk_size"]

    ranges: list[tuple[int, int]] = []
    waiting: list[tuple[int, int, int]] = []

    for start, end, _ in view.segments:
        tokens = meter.text(view.text[start:end])
        if tokens > maximum:
            ranges.extend(pack(waiting, size, 0))
            waiting = []
            pieces = sentence_units(view.text, start, end, meter, maximum)
            ranges.extend(pack(pieces, size, overlap))
        else:
            waiting.append((start, end, tokens))

    ranges.extend(pack(waiting, size, 0))
    ranges = absorb_small_tail(ranges, view.text, meter, params["min_chunk_size"], maximum)
    return make_chunks(view, ranges)


def _split_range(text: str, start: int, end: int, params: dict, meter: Meter) -> list[tuple[int, int]]:
    """The recursive split of one range: separators in order, merged up to the
    size, with the overlap."""

    leaves = leaf_ranges(text, start, end, params["chunk_size"], meter)
    ranges = pack(_units(text, leaves, meter), params["chunk_size"], params["chunk_overlap"])
    return absorb_small_tail(ranges, text, meter, params["min_chunk_size"], params["max_chunk_size"])


def recursive(blocks: list[Block], params: dict, indices: list[int] | None = None) -> list[Chunk]:
    """Also the universal fallback, so it covers every block it is given."""

    view = TextView(blocks, indices)
    meter = Meter()
    return make_chunks(view, _split_range(view.text, 0, len(view.text), params, meter))


def _related(first_path: list[str], other_path: list[str]) -> bool:
    """True when the other section shares the parent of the first one: a
    sibling, or something below it. Never a shallower heading."""

    if not first_path:
        return True
    return other_path[: len(first_path) - 1] == first_path[:-1]


def heading_aware(blocks: list[Block], params: dict, indices: list[int] | None = None) -> list[Chunk]:
    """One chunk per section. Small neighbours merge, big sections are split."""

    view = TextView(blocks, indices)
    meter = Meter()
    text = view.text
    minimum, maximum = params["min_chunk_size"], params["max_chunk_size"]

    # Sections: a heading starts a new one
    sections: list[tuple[int, int, list[str]]] = []  # (start, end, path)
    first = 0
    for number, (_, _, block_index) in enumerate(view.segments):
        if blocks[block_index].type == "heading" and number > first:
            sections.append((first, number - 1, blocks[view.segments[first][2]].section_path))
            first = number
    if view.segments:
        sections.append((first, len(view.segments) - 1, blocks[view.segments[first][2]].section_path))

    ranges: list[tuple[int, int]] = []
    group: tuple[int, int, int, list[str]] | None = None  # (start, end, tokens, path)

    def flush() -> None:
        nonlocal group
        if group:
            ranges.append((group[0], group[1]))
            group = None

    for first_seg, last_seg, path in sections:
        start, end = view.segments[first_seg][0], view.segments[last_seg][1]
        tokens = meter.text(text[start:end])

        if group and group[2] < minimum and _related(group[3], path):
            if tokens <= maximum and group[2] + tokens <= maximum:
                group = (group[0], end, group[2] + tokens, group[3])
                continue
            if tokens > maximum:
                # a tiny heading joins the big section below it before the split
                start, tokens, path = group[0], group[2] + tokens, group[3]
                group = None

        flush()

        if tokens <= maximum:
            group = (start, end, tokens, path)
        else:
            ranges.extend(_split_range(text, start, end, params, meter))

    flush()
    return make_chunks(view, ranges)
