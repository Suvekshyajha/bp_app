"""Stage 2: turn a parsed document into chunks, no LLM involved (plan 15.4-15.6)."""

import statistics
from dataclasses import dataclass

from analysis_schema import DEFAULT_PARAMS, Analysis
from chunking import strategies
from chunking.hybrid import hybrid as hybrid_chunker
from chunking.semantic import semantic as semantic_chunker
from chunking.strategies import ChunkingFallback
from chunking.validate import ChunkingError, ChunkingFailed, TooManyChunks, find_problem
from doc_model import Chunk, ParsedDocument

STRATEGIES = {
    "single_chunk": strategies.single_chunk,
    "fixed_size": strategies.fixed_size,
    "sentence": strategies.sentence,
    "paragraph": strategies.paragraph,
    "recursive": strategies.recursive,
    "heading_aware": strategies.heading_aware,
    "sliding_window": strategies.sliding_window,
    "semantic": semantic_chunker,
    "hybrid": hybrid_chunker,
}


@dataclass
class ChunkResult:
    chunks: list[Chunk]
    report: dict


def _add_reason(existing: str, new: str) -> str:
    return new if not existing else f"{existing};{new}"


def run_chunking(parsed: ParsedDocument, analysis: Analysis) -> ChunkResult:
    """Chunk the document the way the analysis says, and check the result.

    If the chosen chunker cannot be used or its chunks fail the checks, the
    recursive chunker runs with the default parameters instead and the reason
    is recorded. Raises ChunkingError when even that fails.
    """

    blocks = parsed.blocks
    used = analysis.strategy
    params = dict(analysis.params)
    reason = analysis.fallback_reason

    chunks = None
    chunker = STRATEGIES.get(used)
    if chunker is None:
        reason = _add_reason(reason, f"unsupported_strategy:{used[:40]}")
        used, params = "recursive", dict(DEFAULT_PARAMS)
    else:
        try:
            chunks = chunker(blocks, params)
        except ChunkingFallback as error:
            reason = _add_reason(reason, error.reason)
            used, params, chunks = "recursive", dict(DEFAULT_PARAMS), None

    if chunks is None:
        chunks = strategies.recursive(blocks, params)

    problem = find_problem(chunks, blocks, params)

    if problem:
        already_default = used == "recursive" and params == DEFAULT_PARAMS
        if already_default:
            raise ChunkingFailed()

        reason = _add_reason(reason, f"invalid_chunks:{problem}")
        used, params = "recursive", dict(DEFAULT_PARAMS)
        chunks = strategies.recursive(blocks, params)

        if find_problem(chunks, blocks, params):
            raise ChunkingFailed()

    for number, chunk in enumerate(chunks):
        chunk.index = number

    sizes = sorted(chunk.tokens for chunk in chunks)
    report = {
        "analysis_source": analysis.source,
        "requested_strategy": analysis.requested_strategy or analysis.strategy,
        "used_strategy": used,
        "params_requested": analysis.params_requested,
        "params_used": {
            "chunk_size": params["chunk_size"],
            "chunk_overlap": params["chunk_overlap"],
            "min": params["min_chunk_size"],
            "max": params["max_chunk_size"],
        },
        "clamped": analysis.clamped,
        "fallback_reason": reason,
        "chunk_count": len(chunks),
        "token_stats": {
            "min": sizes[0],
            "median": int(statistics.median(sizes)),
            "max": sizes[-1],
        },
    }
    return ChunkResult(chunks=chunks, report=report)
