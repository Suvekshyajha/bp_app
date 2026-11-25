"""Document chunks in Qdrant (the `document_chunks` collection).

Every chunk point carries user_id and document_id, and every read, update and
delete here filters on them, so one user's work never touches another's.
Text fields are encrypted with the same key as the rest of the system.

Chunks are stored with status "processing" and flipped to "ready" in ONE call
after all of them are in, so the retriever (which only searches "ready") never
sees half a document.
"""

import os
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from qdrant_client import QdrantClient, models

from common.crypto import encrypt
from common.settings import CHUNKS_COLLECTION, DENSE_DIM, DOC_NAMESPACE
from doc_model import Chunk

load_dotenv()

QDRANT_URL = os.getenv("QDRANT_URL", "").strip()
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None

if not QDRANT_URL:
    raise ValueError("QDRANT_URL is not configured")

COLLECTION_NAME = CHUNKS_COLLECTION

client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=30)


def _document_filter(user_id: str, document_id: str) -> models.Filter:
    return models.Filter(
        must=[
            models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id)),
            models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id)),
        ]
    )


def chunk_point_id(user_id: str, file_hash: str, index: int) -> str:
    """The same user, file and chunk number always give the same id, so running
    a job again replaces its chunks instead of adding copies."""

    return str(uuid.uuid5(DOC_NAMESPACE, f"{user_id}:{file_hash}:{index}"))


def ensure_collection() -> None:
    """Create the collection and its filter indexes if they are missing."""

    if client.collection_exists(COLLECTION_NAME):
        info = client.get_collection(COLLECTION_NAME)
        size = info.config.params.vectors["dense"].size
        if size != DENSE_DIM:
            raise RuntimeError(
                f"Collection '{COLLECTION_NAME}' has vectors of size {size}, "
                f"but the embedding model makes {DENSE_DIM}."
            )
        return

    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config={
            "dense": models.VectorParams(size=DENSE_DIM, distance=models.Distance.COSINE)
        },
        sparse_vectors_config={
            "sparse": models.SparseVectorParams(
                index=models.SparseIndexParams(on_disk=False),
                modifier=models.Modifier.IDF,  # BM25 needs IDF at query time
            )
        },
    )

    for field in ("user_id", "document_id", "status"):
        client.create_payload_index(
            collection_name=COLLECTION_NAME,
            field_name=field,
            field_schema=models.PayloadSchemaType.KEYWORD,
        )


def upsert_chunks(
    user_id: str,
    document_id: str,
    file_hash: str,
    filename: str,
    strategy: str,
    chunks: list[Chunk],
    vectors: list[dict],
) -> None:
    """Store chunks (already embedded) with status "processing"."""

    created_at = datetime.now(timezone.utc).isoformat()
    encrypted_filename = encrypt(filename)

    points = []
    for chunk, vector in zip(chunks, vectors):
        points.append(
            models.PointStruct(
                id=chunk_point_id(user_id, file_hash, chunk.index),
                vector={
                    "dense": vector["dense"],
                    "sparse": models.SparseVector(
                        indices=vector["sparse"]["indices"],
                        values=vector["sparse"]["values"],
                    ),
                },
                payload={
                    "user_id": user_id,
                    "document_id": document_id,
                    "chunk_index": chunk.index,
                    "filename": encrypted_filename,
                    "section_title": encrypt(chunk.section_title) if chunk.section_title else None,
                    "page": chunk.page,
                    "content": encrypt(chunk.text),
                    "strategy": strategy,
                    "status": "processing",
                    "created_at": created_at,
                },
            )
        )

    client.upsert(collection_name=COLLECTION_NAME, points=points, wait=True)


def mark_ready(user_id: str, document_id: str) -> None:
    """Make every chunk of the document searchable, in one call."""

    client.set_payload(
        collection_name=COLLECTION_NAME,
        payload={"status": "ready"},
        points=_document_filter(user_id, document_id),
        wait=True,
    )


def delete_document(user_id: str, document_id: str) -> None:
    """Remove every chunk of one user's document. Safe to call twice."""

    if not client.collection_exists(COLLECTION_NAME):
        return

    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=models.FilterSelector(filter=_document_filter(user_id, document_id)),
        wait=True,
    )
