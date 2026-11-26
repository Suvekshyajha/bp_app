"""semantic: cut where the meaning changes (plan 15.4).

Each sentence is embedded together with its neighbours. Where two consecutive
windows are far apart (above a percentile of all distances), a new chunk
starts. Costly, so it only runs under the size caps in limits.py.
"""

import math

import limits
from chunking.strategies import (
    ChunkingFallback,
    Meter,
    TextView,
    make_chunks,
    recursive,
    sentence_ranges,
    leaf_ranges,
)
from doc_model import Block, Chunk

_embedder = None


def set_embedder(embedder) -> None:
    """Use `embedder(list[str]) -> list[{"dense": [...]}]`. None restores the
    real one. Tests use this so no model is loaded."""

    global _embedder
    _embedder = embedder


def _embed(texts: list[str]) -> list[list[float]]:
    if _embedder is not None:
        return [item["dense"] for item in _embedder(texts)]

    from common.embeddings import embed_passages

    return [item["dense"] for item in embed_passages(texts)]


def _distance(first: list[float], second: list[float]) -> float:
    dot = sum(a * b for a, b in zip(first, second))
    norm = math.sqrt(sum(a * a for a in first)) * math.sqrt(sum(b * b for b in second))
    return 1.0 - (dot / norm if norm else 0.0)


def _percentile(values: list[float], percent: int) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    low = int(math.floor(position))
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def semantic(blocks: list[Block], params: dict, indices: list[int] | None = None) -> list[Chunk]:
    view = TextView(blocks, indices)
    text = view.text
    meter = Meter()
    maximum, minimum = params["max_chunk_size"], params["min_chunk_size"]

    if len(text) > limits.SEMANTIC_MAX_CHARS:
        raise ChunkingFallback("semantic_too_large")

    sentences = sentence_ranges(text)
    if len(sentences) > limits.SEMANTIC_MAX_SENTENCES:
        raise ChunkingFallback("semantic_too_large")
    if len(sentences) < 3:
        return recursive(blocks, params, indices)

    # Each sentence together with the one before and the one after
    windows = []
    for number in range(len(sentences)):
        around = sentences[max(0, number - 1) : number + 2]
        windows.append(" ".join(text[a:b] for a, b in around))

    vectors = _embed(windows)
    distances = [_distance(vectors[i], vectors[i + 1]) for i in range(len(vectors) - 1)]
    threshold = _percentile(distances, limits.SEMANTIC_PERCENTILE)
    cut_after = [distance > threshold for distance in distances] + [False]

    # Units: sentences, with an over-long one split by the recursive separators
    units: list[tuple[int, int, int]] = []
    unit_cut: list[bool] = []
    for number, (a, b) in enumerate(sentences):
        tokens = meter.text(text[a:b])
        pieces = [(a, b)] if tokens <= maximum else leaf_ranges(text, a, b, maximum, meter)
        for position, (x, y) in enumerate(pieces):
            units.append((x, y, meter.text(text[x:y])))
            unit_cut.append(cut_after[number] and position == len(pieces) - 1)

    # Segments end at a cut, or before they would pass the maximum
    groups: list[list[int]] = []  # [start, end, tokens]
    for number, (a, b, tokens) in enumerate(units):
        if groups and not unit_cut[number - 1] and groups[-1][2] + tokens <= maximum:
            groups[-1][1] = b
            groups[-1][2] += tokens
        else:
            groups.append([a, b, tokens])

    # Small segments join a neighbour when the two still fit together
    merged: list[list[int]] = []
    for group in groups:
        if merged and (group[2] < minimum or merged[-1][2] < minimum) and merged[-1][2] + group[2] <= maximum:
            merged[-1][1] = group[1]
            merged[-1][2] += group[2]
        else:
            merged.append(group)

    return make_chunks(view, [(a, b) for a, b, _ in merged])
