import os
import uuid
from datetime import datetime, timezone
from crypto import encrypt
from dotenv import load_dotenv
from qdrant_client import QdrantClient, models

from embeddings import embed_messages


load_dotenv()

QDRANT_URL = os.getenv("QDRANT_URL")

if not QDRANT_URL:
    raise ValueError("QDRANT_URL is not configured")


COLLECTION_NAME = "chat_messages"

client = QdrantClient(url=QDRANT_URL)


def get_qdrant_client() -> QdrantClient:
    return client


def check_connection() -> bool:
    try:
        client.get_collections()
        return True
    except Exception:
        return False


def create_collection() -> None:
    """Create the chat messages collection if it doesn't exist."""

    if client.collection_exists(COLLECTION_NAME):
        print(f"Collection '{COLLECTION_NAME}' already exists.")
        return

    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config={
            "dense": models.VectorParams(
                size=768,
                distance=models.Distance.COSINE,
            )
        },
        sparse_vectors_config={
            "sparse": models.SparseVectorParams(
                index=models.SparseIndexParams(
                    on_disk=False,
                ),
                # BM25 needs Qdrant to apply IDF at query time
                modifier=models.Modifier.IDF,
            )
        },
    )

    # Create indexes for filtering messages
    client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="user_id",
        field_schema=models.PayloadSchemaType.KEYWORD,
    )

    client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="conversation_id",
        field_schema=models.PayloadSchemaType.KEYWORD,
    )

    print(f"Collection '{COLLECTION_NAME}' created successfully.")


def store_message(
    user_id: str,
    conversation_id: str,
    role: str,
    content: str,
) -> str:
    """Generate embeddings and store a chat message in Qdrant."""

    message_id = str(uuid.uuid4())

    vectors = embed_messages([content])[0]

    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            models.PointStruct(
                id=message_id,
                vector={
                    "dense": vectors["dense"],
                    "sparse": models.SparseVector(
                        indices=vectors["sparse"]["indices"],
                        values=vectors["sparse"]["values"],
                    ),
                },
                payload={
                    "message_id": message_id,
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "role": role,
                    "content": encrypt(content),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        ],
    )

    return message_id