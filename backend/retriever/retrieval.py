
from qdrant_client import models

from embeddings import embed_query
from qdrant_store import client, COLLECTION_NAME


def retrieve_messages(
    query: str,
    user_id: str,
    limit: int = 5,
) -> list[dict]:
    """Retrieve relevant past chat messages using hybrid search."""

    vectors = embed_query(query)

    dense_vector = vectors["dense"]

    sparse_vector = models.SparseVector(
        indices=vectors["sparse"]["indices"],
        values=vectors["sparse"]["values"],
    )

    # Restrict retrieval to the authenticated user's messages
    query_filter = models.Filter(
        must=[
            models.FieldCondition(
                key="user_id",
                match=models.MatchValue(value=user_id),
            )
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
