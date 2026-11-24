"""Dense and sparse embeddings, shared by the retriever and the ingestion worker.

The models load on first use, not when this file is imported, so importing it
is cheap and never needs a model download. The retriever calls warm_up() at
start so the first chat turn is not slow. Services that must stay light (the
orchestrator and the ingestion API process) must never call anything here.
"""

import threading

from common.settings import DENSE_MODEL, SPARSE_MODEL

__all__ = [
    "get_dense",
    "get_sparse",
    "warm_up",
    "embed_messages",
    "embed_passages",
    "embed_query",
    "count_tokens",
]

_dense = None
_sparse = None
_load_lock = threading.Lock()


def get_dense():
    """The dense model, loaded once on first use."""

    global _dense

    if _dense is None:
        with _load_lock:
            if _dense is None:
                from sentence_transformers import SentenceTransformer

                _dense = SentenceTransformer(DENSE_MODEL)

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
    """Load both models now (call at start up)."""

    get_dense()
    get_sparse()


def embed_messages(messages: list[str]) -> list[dict]:
    """Generate dense and sparse embeddings for chat messages."""

    if not messages:
        return []

    dense_vectors = get_dense().encode(
        messages,
        normalize_embeddings=True,
    )

    sparse_vectors = list(get_sparse().embed(messages))

    results = []

    for dense, sparse in zip(dense_vectors, sparse_vectors):
        results.append({
            "dense": dense.tolist(),
            "sparse": {
                "indices": sparse.indices.tolist(),
                "values": sparse.values.tolist(),
            },
        })

    return results


# Document chunks are embedded exactly like messages (no query prefix).
embed_passages = embed_messages


def embed_query(text: str) -> dict:
    """Generate embeddings for a conversational search query."""

    query = (
        "Represent this sentence for searching relevant passages: "
        + text
    )

    dense_vector = get_dense().encode(
        query,
        normalize_embeddings=True,
    )

    sparse_vector = list(get_sparse().query_embed(text))[0]

    return {
        "dense": dense_vector.tolist(),
        "sparse": {
            "indices": sparse_vector.indices.tolist(),
            "values": sparse_vector.values.tolist(),
        },
    }


def count_tokens(text: str) -> int:
    """Tokens the dense model would read (no special tokens added).

    Chunk sizes are measured in these, because bge-base reads at most 512.
    """

    return len(get_dense().tokenizer.encode(text, add_special_tokens=False))
