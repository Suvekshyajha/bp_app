"""Search the chunks of the user's documents (read only).

The ingestion service writes the `document_chunks` collection. This file only
reads it. Every filter here holds the user id, so an id that belongs to someone
else behaves exactly like an id that does not exist.

This file does not import retrieval.py on purpose: it stays small and has no
other dependency than Qdrant, the encryption helpers and the cleaning helpers.
"""

import logging
import os

from qdrant_client import models

from common.sanitize import clean_for_prompt
from common.settings import CHUNKS_COLLECTION
from crypto import decrypt
from qdrant_store import client

logger = logging.getLogger(__name__)

# Same meaning as in retrieval.py: the dense cosine similarity a chunk must
# reach to be returned. Tune it from the "Document retrieval:" log line.
MIN_SIMILARITY = float(os.getenv("RETRIEVAL_MIN_SIMILARITY", "0.5"))

# Search this many times `limit` candidates, so dropping weak ones still
# leaves enough good ones.
CANDIDATE_FACTOR = 3

READY = "ready"
PROCESSING = "processing"
MISSING = "missing"


def build_filter(
    user_id: str,
    document_ids: list[str] | None = None,
    ready_only: bool = True,
) -> models.Filter:
    """A filter that always holds the user id.

    With `document_ids` only those documents match. With `ready_only` only
    chunks of finished documents match (the ingestion worker marks a
    document's chunks "ready" in one call, after the last one is stored).
    """

    # Without a user id the filter would be too wide: refuse instead of leaking
    if not user_id:
        raise ValueError("user_id is required")

    must = [
        models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id)),
    ]

    if document_ids is not None:
        must.append(
            models.FieldCondition(
                key="document_id",
                match=models.MatchAny(any=list(document_ids)),
            )
        )

    if ready_only:
        must.append(
            models.FieldCondition(key="status", match=models.MatchValue(value=READY))
        )

    return models.Filter(must=must)


def _collection_exists() -> bool:
    return client.collection_exists(CHUNKS_COLLECTION)


def _count(query_filter: models.Filter) -> int:
    return client.count(
        collection_name=CHUNKS_COLLECTION,
        count_filter=query_filter,
        exact=True,
    ).count


def document_states(user_id: str, document_ids: list[str]) -> dict[str, str]:
    """For each id: "ready", "processing" or "missing".

    "missing" also covers the documents of other users (the user id is always
    in the filter) and documents whose chunks are not stored yet.
    """

    if not user_id:
        raise ValueError("user_id is required")

    # Nothing has been ingested yet: no document exists
    if not _collection_exists():
        return {document_id: MISSING for document_id in document_ids}

    states: dict[str, str] = {}

    for document_id in document_ids:
        if _count(build_filter(user_id, [document_id], ready_only=True)) > 0:
            states[document_id] = READY
        elif _count(build_filter(user_id, [document_id], ready_only=False)) > 0:
            states[document_id] = PROCESSING
        else:
            states[document_id] = MISSING

    return states


def _dot(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two unit-length vectors (both are normalized)."""

    return sum(x * y for x, y in zip(a, b))


def _dense_vector_of(point) -> list[float] | None:
    vector = point.vector

    if isinstance(vector, dict):  # named vectors come back as {"dense": [...]}
        vector = vector.get("dense")

    return vector or None


def _plain(value) -> str:
    """Decrypt a stored text field and make it safe to place in a prompt."""

    if not value:
        return ""

    return clean_for_prompt(decrypt(value))


def retrieve_document_chunks(
    query_vectors: dict,
    user_id: str,
    document_ids: list[str],
    limit: int = 6,
    min_similarity: float | None = None,
) -> list[dict]:
    """The chunks of these documents that best match the question.

    `query_vectors` is the result of embed_query (the question is embedded
    once and shared with the chat memory search). The result has the same
    shape as retrieve_messages: [{"id", "score", "payload"}], best first,
    where `score` is the dense similarity. The payload carries `content`,
    `filename`, `document_id`, `chunk_index`, `section_title`, `page` and
    `timestamp`.
    """

    if not user_id:
        raise ValueError("user_id is required")

    if not document_ids:
        return []

    # No document was ever ingested: behave as "no documents", never crash
    if not _collection_exists():
        return []

    threshold = MIN_SIMILARITY if min_similarity is None else min_similarity
    candidates = limit * CANDIDATE_FACTOR

    dense_vector = query_vectors["dense"]
    sparse_vector = models.SparseVector(
        indices=query_vectors["sparse"]["indices"],
        values=query_vectors["sparse"]["values"],
    )

    query_filter = build_filter(user_id, document_ids, ready_only=True)

    results = client.query_points(
        collection_name=CHUNKS_COLLECTION,
        prefetch=[
            models.Prefetch(
                query=dense_vector,
                using="dense",
                limit=candidates,
                filter=query_filter,
            ),
            models.Prefetch(
                query=sparse_vector,
                using="sparse",
                limit=candidates,
                filter=query_filter,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        query_filter=query_filter,
        limit=candidates,
        with_payload=True,
        with_vectors=["dense"],  # needed to measure the similarity
    )

    kept: list[dict] = []
    dropped: list[float] = []

    for point in results.points:
        stored = _dense_vector_of(point)
        similarity = _dot(dense_vector, stored) if stored else 0.0

        if similarity < threshold:
            dropped.append(similarity)
            continue

        if len(kept) < limit:
            payload = point.payload or {}

            kept.append({
                "id": str(point.id),
                "score": similarity,
                "payload": {
                    "document_id": payload.get("document_id", ""),
                    "filename": _plain(payload.get("filename")),
                    "chunk_index": payload.get("chunk_index", 0),
                    "section_title": _plain(payload.get("section_title")),
                    "page": payload.get("page"),
                    "content": _plain(payload.get("content")),
                    "timestamp": payload.get("created_at"),
                },
            })

    logger.info(
        "Document retrieval: kept %d, dropped %d below %.2f (best dropped: %s)",
        len(kept),
        len(dropped),
        threshold,
        f"{max(dropped):.2f}" if dropped else "-",
    )

    return kept
