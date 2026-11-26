"""One token counter for all of ingestion.

Production counts with the dense model's tokenizer (common.embeddings.count_tokens).
That import loads a model, so it happens on first use and never at import,
and tests replace the counter with a cheap one through `set_counter()`.
"""

from typing import Callable

_counter: Callable[[str], int] | None = None


def set_counter(counter: Callable[[str], int] | None) -> None:
    """Use `counter` instead of the real tokenizer. None restores the real one."""

    global _counter
    _counter = counter


def count_tokens(text: str) -> int:
    if _counter is not None:
        return _counter(text)

    from common.embeddings import count_tokens as real_count_tokens

    return real_count_tokens(text)
