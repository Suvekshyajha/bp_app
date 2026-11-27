import logging
import os
from common.sanitize import (  # noqa: F401  (re-exported for main.py and tests)
    DELIMITER_TAG,
    HIDDEN_CHARACTERS,
    clean_for_prompt,
    strip_hidden_characters,
)
from crypto import decrypt
from qdrant_client import models

from embeddings import embed_query
from qdrant_store import client, COLLECTION_NAME


logger = logging.getLogger(__name__)

# How alike (cosine similarity, 0 to 1) a stored message must be to the
# question to be returned. Weaker matches are dropped, so unrelated chat
# history is never handed to the model.
#
# The hybrid (RRF) score cannot be used for this: it only reflects rank
# and is tiny for every result. The dense cosine similarity has a stable
# scale. Tune the value from the "Retrieval:" log line below.
#
# The scale belongs to the model. This default has NOT been tuned for
# gemini-embedding-2: check the log line on real questions and adjust it.
MIN_SIMILARITY = float(os.getenv("RETRIEVAL_MIN_SIMILARITY", "0.5"))

# Search this many times `limit` candidates, so that dropping weak ones
# still leaves enough good ones to fill `limit`.
CANDIDATE_FACTOR = 3


# The prompt-injection cleaning lives in common/sanitize.py now. The names
# are imported above, so `retrieval.clean_for_prompt` etc. keep working.


def dot(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two unit-length vectors (both are normalized)."""
    return sum(x * y for x, y in zip(a, b))


def dense_vector_of(point) -> list[float] | None:
    vector = point.vector

    if isinstance(vector, dict):  # named vectors come back as {"dense": [...]}
        vector = vector.get("dense")

    return vector or None


def retrieve_messages(
    query: str,
    user_id: str,
    conversation_id: str,
    limit: int = 5,
    min_similarity: float | None = None,
    vectors: dict | None = None,
) -> list[dict]:
    """Retrieve relevant messages of ONE chat of ONE user (hybrid search).

    Candidates are ranked by hybrid search (best match first). Those whose
    dense similarity to the question is below the threshold are dropped.
    `score` in the result is that similarity, not the fusion score.

    `vectors` is the question already embedded (the result of embed_query).
    Pass it when another search needs the same vectors, so the question is
    embedded only once. Without it the question is embedded here.
    """

    # Without both ids the filter would be too wide: refuse instead of leaking
    if not user_id or not conversation_id:
        raise ValueError("user_id and conversation_id are required")

    threshold = MIN_SIMILARITY if min_similarity is None else min_similarity
    candidates = limit * CANDIDATE_FACTOR

    if vectors is None:
        vectors = embed_query(query)

    dense_vector = vectors["dense"]

    sparse_vector = models.SparseVector(
        indices=vectors["sparse"]["indices"],
        values=vectors["sparse"]["values"],
    )

    # Restrict retrieval to this user's messages in this one chat
    query_filter = models.Filter(
        must=[
            models.FieldCondition(
                key="user_id",
                match=models.MatchValue(value=user_id),
            ),
            models.FieldCondition(
                key="conversation_id",
                match=models.MatchValue(value=conversation_id),
            ),
        ]
    )

    results = client.query_points(
        collection_name=COLLECTION_NAME,
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
        stored = dense_vector_of(point)
        similarity = dot(dense_vector, stored) if stored else 0.0

        if similarity < threshold:
            dropped.append(similarity)
            continue

        if len(kept) < limit:
            kept.append({
                "id": str(point.id),
                "score": similarity,
                "payload": {
                    **point.payload,
                    "content": clean_for_prompt(
                        decrypt(point.payload.get("content", ""))
                    ),
                },
            })

    logger.info(
        "Retrieval: kept %d, dropped %d below %.2f (best dropped: %s)",
        len(kept),
        len(dropped),
        threshold,
        f"{max(dropped):.2f}" if dropped else "-",
    )

    return kept