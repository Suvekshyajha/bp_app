"""Deterministic statistics about a parsed document (plan section 15.2).

Pure Python, no model calls. The same input always gives the same numbers.
Stage 1 (the analyzer) sends these numbers to the LLM instead of the file.
"""

from chunking.tokens import count_tokens
from doc_model import ParsedDocument

BLOCK_TYPES = ("heading", "paragraph", "list", "table", "code")


def compute_stats(parsed: ParsedDocument) -> dict:
    counts = dict.fromkeys(BLOCK_TYPES, 0)
    chars = 0
    tokens = 0
    paragraph_tokens: list[int] = []
    heading_levels: list[int] = []

    for block in parsed.blocks:
        size = count_tokens(block.text)
        chars += len(block.text)
        tokens += size

        if block.type in counts:
            counts[block.type] += 1

        if block.type == "paragraph":
            paragraph_tokens.append(size)
        elif block.type == "heading":
            heading_levels.append(block.level or 1)

    total_blocks = sum(counts.values())

    # Depth = how many levels the headings span (H1 to H3 is 3, only H3s is 1)
    heading_depth = (
        max(heading_levels) - min(heading_levels) + 1 if heading_levels else 0
    )

    # At least 3 headings, and either 2 or more levels or 3 headings sharing one
    same_level_max = max(
        (heading_levels.count(level) for level in set(heading_levels)), default=0
    )
    has_hierarchy = len(heading_levels) >= 3 and (
        len(set(heading_levels)) >= 2 or same_level_max >= 3
    )

    return {
        "source_type": parsed.source_type,
        "chars": chars,
        "tokens": tokens,
        "pages": parsed.page_count,
        "blocks": counts,
        "heading_depth": heading_depth,
        "avg_paragraph_tokens": (
            round(sum(paragraph_tokens) / len(paragraph_tokens)) if paragraph_tokens else 0
        ),
        "max_paragraph_tokens": max(paragraph_tokens, default=0),
        "list_ratio": round(counts["list"] / total_blocks, 2) if total_blocks else 0.0,
        "has_hierarchy": has_hierarchy,
    }
