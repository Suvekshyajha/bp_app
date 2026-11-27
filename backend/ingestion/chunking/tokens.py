"""One token counter for all of ingestion.

Production uses the local estimate in common.embeddings.count_tokens (no model
and no API call). Tests replace the counter through `set_counter()`.
"""

from typing import Callable

_counter: Callable[[str], int] | None = None


def set_counter(counter: Callable[[str], int] | None) -> None:
    """Use `counter` instead of the estimate. None restores the estimate."""

    global _counter
    _counter = counter


def count_tokens(text: str) -> int:
    if _counter is not None:
        return _counter(text)

    from common.embeddings import count_tokens as real_count_tokens

    return real_count_tokens(text)
