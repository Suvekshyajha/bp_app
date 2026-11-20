from qdrant_client import models

from embeddings import embed_query
from qdrant_store import client, COLLECTION_NAME


def retrieve_messages(
    query: str,
    user_id: str,
    conversation_id: str,
    limit: int = 5,
) -> list[dict]:
    """Retrieve relevant messages of ONE chat of ONE user (hybrid search)."""

    # Without both ids the filter would be too wide: refuse instead of leaking
    if not user_id or not conversation_id:
        raise ValueError("user_id and conversation_id are required")

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
                limit=limit * 2,
                filter=query_filter,
            ),
            models.Prefetch(
                query=sparse_vector,
                using="sparse",
                limit=limit * 2,
                filter=query_filter,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        query_filter=query_filter,
        limit=limit,
        with_payload=True,
    )

    return [
        {
            "id": str(point.id),
            "score": point.score,
            "payload": point.payload,
        }
        for point in results.points
    ]