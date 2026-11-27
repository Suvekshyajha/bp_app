"""Dense and sparse embeddings, shared by the retriever and the ingestion worker.

Dense vectors (meaning) come from the Gemini API, so no large model runs here.
Sparse vectors (BM25 keywords) are made on this machine.

Nothing loads on import: the HTTP client and the BM25 model are created on
first use. Services that must stay light (the orchestrator and the ingestion
API process) must never call anything here.

The only setting is GEMINI_API_KEY. The texts sent to Gemini are readable
text, even though this app stores them encrypted.
"""

import logging
import math
import os
import threading
import time

from common.settings import DENSE_DIM, DENSE_MAX_CHARS, DENSE_MODEL, SPARSE_MODEL

__all__ = [
    "EmbeddingError",
    "require_api_key",
    "get_dense",
    "get_sparse",
    "warm_up",
    "embed_messages",
    "embed_passages",
    "embed_query",
    "count_tokens",
]

logger = logging.getLogger(__name__)

API_URL = "https://generativelanguage.googleapis.com/v1beta"
BATCH_SIZE = 100  # texts in one request

# gemini-embedding-2 is told the task in the text itself
QUERY_FORMAT = "task: search result | query: {}"
PASSAGE_FORMAT = "title: none | text: {}"

RETRY_STATUSES = (429, 500, 502, 503, 504)

# (attempts, longest wait between two attempts, request timeout), in seconds
QUERY_POLICY = (2, 1.0, 10.0)  # someone is waiting for the answer
MESSAGE_POLICY = (3, 4.0, 15.0)  # holds one of the retriever's job slots
PASSAGE_POLICY = (7, 60.0, 30.0)  # background work: outlast a per-minute limit

# Token estimate used for chunk sizes (see count_tokens)
CHARS_PER_TOKEN = 4

_dense = None
_sparse = None
_load_lock = threading.Lock()


class EmbeddingError(RuntimeError):
    """The Gemini API could not make the vectors. Never holds any user text."""


def require_api_key() -> str:
    """The key from GEMINI_API_KEY. Raises if it is missing."""

    key = os.getenv("GEMINI_API_KEY", "").strip()

    if not key or key == "your_key_here":
        raise EmbeddingError(
            "GEMINI_API_KEY is not set. Make a key in Google AI Studio and add it."
        )

    return key


def get_dense():
    """The HTTP client for the Gemini API, created once on first use."""

    global _dense

    if _dense is None:
        with _load_lock:
            if _dense is None:
                import httpx

                _dense = httpx.Client(
                    base_url=API_URL,
                    headers={"x-goog-api-key": require_api_key()},
                )

    return _dense


def get_sparse():
    """The sparse (BM25) model, loaded once on first use."""

    global _sparse

    if _sparse is None:
        with _load_lock:
            if _sparse is None:
                from fastembed import SparseTextEmbedding

                _sparse = SparseTextEmbedding(model_name=SPARSE_MODEL)

    return _sparse


def warm_up() -> None:
    """Check the API key and load the BM25 model (call at start up)."""

    require_api_key()
    get_sparse()


def _retry_delay(response, attempt: int, longest: float) -> float:
    """Wait longer after each failure, or as long as the server asks."""

    delay = 2.0 ** attempt

    if response is not None:
        try:
            delay = max(delay, float(response.headers.get("retry-after", "")))
        except ValueError:
            pass

    return min(delay, longest)


def _post(body: dict, policy: tuple[int, float, float]) -> dict:
    """One batch request, retried when the failure may pass."""

    import httpx

    attempts, longest_wait, timeout = policy
    client = get_dense()
    path = f"/models/{DENSE_MODEL}:batchEmbedContents"

    for attempt in range(attempts):
        response = None

        try:
            response = client.post(path, json=body, timeout=timeout)
        except httpx.HTTPError as error:
            problem = type(error).__name__
        else:
            if response.status_code == 200:
                return response.json()

            problem = f"HTTP {response.status_code}"

            if response.status_code not in RETRY_STATUSES:
                break

        if attempt + 1 < attempts:
            delay = _retry_delay(response, attempt, longest_wait)
            logger.warning("Gemini embedding failed (%s); retrying in %.0f s", problem, delay)
            time.sleep(delay)

    raise EmbeddingError(f"Gemini embedding failed: {problem}")


def _normalize(values: list[float]) -> list[float]:
    """Unit length: the searches compare vectors with a plain dot product."""

    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values] if norm else values


def _dense_vectors(texts: list[str], policy: tuple[int, float, float]) -> list[list[float]]:
    vectors: list[list[float]] = []

    for first in range(0, len(texts), BATCH_SIZE):
        batch = texts[first : first + BATCH_SIZE]

        answer = _post(
            {
                "requests": [
                    {
                        "model": f"models/{DENSE_MODEL}",
                        "content": {"parts": [{"text": text}]},
                        "outputDimensionality": DENSE_DIM,
                    }
                    for text in batch
                ]
            },
            policy,
        )

        embeddings = answer.get("embeddings") or []

        if len(embeddings) != len(batch):
            raise EmbeddingError("Gemini returned the wrong number of vectors.")

        for item in embeddings:
            values = item.get("values") or []

            if len(values) != DENSE_DIM:
                raise EmbeddingError(
                    f"Gemini returned a vector of size {len(values)}, expected {DENSE_DIM}."
                )

            vectors.append(_normalize(values))

    return vectors


def _sparse_dict(vector) -> dict:
    return {
        "indices": vector.indices.tolist(),
        "values": vector.values.tolist(),
    }


def _embed_texts(texts: list[str], policy: tuple[int, float, float]) -> list[dict]:
    if not texts:
        return []

    dense_vectors = _dense_vectors(
        [PASSAGE_FORMAT.format(text[:DENSE_MAX_CHARS]) for text in texts],
        policy,
    )

    sparse_vectors = list(get_sparse().embed(texts))

    return [
        {"dense": dense, "sparse": _sparse_dict(sparse)}
        for dense, sparse in zip(dense_vectors, sparse_vectors)
    ]


def embed_messages(messages: list[str]) -> list[dict]:
    """Generate dense and sparse embeddings for chat messages."""

    return _embed_texts(messages, MESSAGE_POLICY)


def embed_passages(passages: list[str]) -> list[dict]:
    """The same vectors for document text, waiting longer for a rate limit."""

    return _embed_texts(passages, PASSAGE_POLICY)


def embed_query(text: str) -> dict:
    """Generate embeddings for a conversational search query."""

    dense_vector = _dense_vectors(
        [QUERY_FORMAT.format(text[:DENSE_MAX_CHARS])],
        QUERY_POLICY,
    )[0]

    sparse_vector = list(get_sparse().query_embed(text))[0]

    return {"dense": dense_vector, "sparse": _sparse_dict(sparse_vector)}


def count_tokens(text: str) -> int:
    """An estimate of the tokens in a text, used for chunk sizes.

    About 4 characters make a token. Counting locally avoids one API request
    per word. Counts add up word by word, which the chunkers rely on.
    """

    return sum(math.ceil(len(word) / CHARS_PER_TOKEN) for word in text.split())
