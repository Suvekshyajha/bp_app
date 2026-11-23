"""Keep the chat history inside what is left of the prompt budget.

Sizes are counted in characters (about 4 characters per token), which
needs no tokenizer and is accurate enough to stay clear of the limit.
"""

# Role label and formatting the API adds around every message
MESSAGE_OVERHEAD_CHARS = 20


def trim_history(history: list[dict], budget_chars: int) -> list[dict]:
    """The newest turns that fit in `budget_chars`, oldest first.

    Turns are dropped from the OLD end, so the chat never has a gap in
    the middle. If nothing fits (zero or negative budget) the result is
    empty.
    """
    kept: list[dict] = []
    used = 0

    for turn in reversed(history):  # newest first
        cost = len(turn["content"]) + MESSAGE_OVERHEAD_CHARS

        if used + cost > budget_chars:
            break

        kept.append(turn)
        used += cost

    kept.reverse()

    return kept