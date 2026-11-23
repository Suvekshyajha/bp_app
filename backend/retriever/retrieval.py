import logging
import os
import re
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
MIN_SIMILARITY = float(os.getenv("RETRIEVAL_MIN_SIMILARITY", "0.5"))

# Search this many times `limit` candidates, so that dropping weak ones
# still leaves enough good ones to fill `limit`.
CANDIDATE_FACTOR = 3


# ---------- Prompt-injection hygiene ----------
#
# Text that comes out of storage is put into the model's prompt, so it is
# cleaned first. Two things are removed:
#
# 1. Characters a person cannot see but a model can read. Attackers hide
#    instructions in them ("ASCII smuggling"): Unicode tag characters, zero
#    width space / word joiner / BOM, bidi overrides and control characters.
#    NOT removed: U+200C and U+200D (zero-width non-joiner / joiner), which
#    Nepali, Hindi, Persian and emoji sequences need to display correctly.
#
# 2. Our own delimiter tag, in any spelling, so a stored message cannot
#    close the <past_messages> block and write text outside it.

HIDDEN_CHARACTERS = re.compile(
    "["
    "\u200b"                # zero-width space
    "\u2060"                # word joiner
    "\ufeff"                # byte order mark
    "\u202a-\u202e"         # bidi embeddings and overrides
    "\u2066-\u2069"         # bidi isolates
    "\U000e0000-\U000e007f"  # Unicode tag characters
    "\U000e0100-\U000e01ef"  # variation selectors supplement
    "\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"  # control characters (not \t \n \r)
    "]"
)

DELIMITER_TAG = re.compile(r"<\s*/?\s*past_messages\b[^>]*>?", re.IGNORECASE)


def strip_hidden_characters(text: str) -> str:
    """Remove invisible characters that can carry hidden instructions."""

    return HIDDEN_CHARACTERS.sub("", text)


def clean_for_prompt(text: str) -> str:
    """Text read from storage, made safe to place inside <past_messages>.

    The cleaning is repeated until the text stops changing. One pass is not
    enough: removing "</past_messages>" from
    "</past_mess</past_messages>ages>" leaves a new working tag behind.
    Every pass only removes characters, so the loop always ends.
    """

    previous = None

    while previous != text:
        previous = text
        text = DELIMITER_TAG.sub("", strip_hidden_characters(text))

    return text


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
) -> list[dict]:
    """Retrieve relevant messages of ONE chat of ONE user (hybrid search).

    Candidates are ranked by hybrid search (best match first). Those whose
    dense similarity to the question is below the threshold are dropped.
    `score` in the result is that similarity, not the fusion score.
    """

    # Without both ids the filter would be too wide: refuse instead of leaking
    if not user_id or not conversation_id:
        raise ValueError("user_id and conversation_id are required")

    threshold = MIN_SIMILARITY if min_similarity is None else min_similarity
    candidates = limit * CANDIDATE_FACTOR

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