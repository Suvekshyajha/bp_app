"""Copy stored vectors to the collections of a new dense model.

Run it ONCE after the model changes, with Qdrant running and the other services
stopped:

    python reembed_collections.py

It reads the old collections (the "_v2" ones if they exist, otherwise the first
ones), decrypts each text, embeds it again and writes it to the new collection
under the same id and with the same (still encrypted) payload. The old
collections are only read, so going back to the old code still works.

If it stops half way, run it again: points already copied are skipped. Do not
run it again after the app has been used with the new model, because anything
deleted since would be copied back. Once you are happy with the results, delete
the old collections: they still hold the old (encrypted) text.
"""

import os

from dotenv import load_dotenv
from qdrant_client import QdrantClient, models

from common.crypto import check_configured, decrypt
from common.settings import CHUNKS_COLLECTION, DENSE_DIM, MESSAGES_COLLECTION

BATCH_SIZE = 32


def _message_text(payload: dict) -> str:
    return decrypt(payload["content"])


def _chunk_text(payload: dict) -> str:
    """The same text the ingestion worker embeds (jobs._embedding_text)."""

    content = decrypt(payload["content"])
    title = payload.get("section_title")

    return f"{decrypt(title)}\n{content}" if title else content


# (old collections newest first, new collection, filter indexes, text to embed)
MOVES = (
    (
        ("chat_messages_v2", "chat_messages"),
        MESSAGES_COLLECTION,
        ("user_id", "conversation_id"),
        _message_text,
    ),
    (
        ("document_chunks_v2", "document_chunks"),
        CHUNKS_COLLECTION,
        ("user_id", "document_id", "status"),
        _chunk_text,
    ),
)


def _create(client: QdrantClient, name: str, indexes: tuple[str, ...]) -> None:
    """The same layout the services create (qdrant_store and qdrant_docs)."""

    client.create_collection(
        collection_name=name,
        vectors_config={
            "dense": models.VectorParams(size=DENSE_DIM, distance=models.Distance.COSINE)
        },
        sparse_vectors_config={
            "sparse": models.SparseVectorParams(
                index=models.SparseIndexParams(on_disk=False),
                modifier=models.Modifier.IDF,
            )
        },
    )

    for field in indexes:
        client.create_payload_index(
            collection_name=name,
            field_name=field,
            field_schema=models.PayloadSchemaType.KEYWORD,
        )


def _move(client, sources, target, indexes, text_of, embed) -> int:
    # Only the newest old collection: an older one can still hold things that
    # were deleted since
    source = next((name for name in sources if client.collection_exists(name)), None)

    if source is None:
        return 0

    if not client.collection_exists(target):
        _create(client, target, indexes)

    copied = 0
    offset = None

    while True:
        points, offset = client.scroll(
            collection_name=source,
            limit=BATCH_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )

        done = set()
        if points:
            done = {
                point.id
                for point in client.retrieve(
                    collection_name=target,
                    ids=[point.id for point in points],
                    with_payload=False,
                )
            }

        missing = [point for point in points if point.id not in done]

        if missing:
            vectors = embed([text_of(point.payload) for point in missing])
            client.upsert(
                collection_name=target,
                points=[
                    models.PointStruct(
                        id=point.id,
                        vector={
                            "dense": vector["dense"],
                            "sparse": models.SparseVector(
                                indices=vector["sparse"]["indices"],
                                values=vector["sparse"]["values"],
                            ),
                        },
                        payload=point.payload,
                    )
                    for point, vector in zip(missing, vectors)
                ],
                wait=True,
            )
            copied += len(missing)

        if offset is None:
            return copied


def reembed(client: QdrantClient, embed) -> dict[str, int]:
    """Move both collections. `embed` is common.embeddings.embed_passages.

    Returns how many points were copied into each new collection.
    """

    return {
        target: _move(client, sources, target, indexes, text_of, embed)
        for sources, target, indexes, text_of in MOVES
    }


def main() -> None:
    load_dotenv()
    check_configured()  # fail before touching Qdrant if the key is missing

    url = os.getenv("QDRANT_URL")
    if not url:
        raise SystemExit("QDRANT_URL is not configured")

    client = QdrantClient(url=url, api_key=os.getenv("QDRANT_API_KEY") or None, timeout=60)

    from common.embeddings import embed_passages

    for name, count in reembed(client, embed_passages).items():
        total = client.count(name).count if client.collection_exists(name) else 0
        print(f"{name}: copied {count}, now holds {total}")


if __name__ == "__main__":
    main()
