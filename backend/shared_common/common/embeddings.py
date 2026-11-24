
from sentence_transformers import SentenceTransformer
from fastembed import SparseTextEmbedding


dense_model = SentenceTransformer("BAAI/bge-base-en-v1.5")

sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")


def embed_messages(messages: list[str]) -> list[dict]:
    """Generate dense and sparse embeddings for chat messages."""

    dense_vectors = dense_model.encode(
        messages,
        normalize_embeddings=True,
    )

    sparse_vectors = list(sparse_model.embed(messages))

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


def embed_query(text: str) -> dict:
    """Generate embeddings for a conversational search query."""

    query = (
        "Represent this sentence for searching relevant passages: "
        + text
    )

    dense_vector = dense_model.encode(
        query,
        normalize_embeddings=True,
    )

    sparse_vector = list(sparse_model.query_embed(text))[0]

    return {
        "dense": dense_vector.tolist(),
        "sparse": {
            "indices": sparse_vector.indices.tolist(),
            "values": sparse_vector.values.tolist(),
        },
    }
