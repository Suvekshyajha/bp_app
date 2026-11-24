"""Settings both services must agree on, so the models cannot drift apart."""

import uuid

DENSE_MODEL = "BAAI/bge-base-en-v1.5"
SPARSE_MODEL = "Qdrant/bm25"
DENSE_DIM = 768

MESSAGES_COLLECTION = "chat_messages"
CHUNKS_COLLECTION = "document_chunks"

# Used to build document and chunk ids (uuid5). NEVER change it: every stored
# id would change and old documents could no longer be found or replaced.
DOC_NAMESPACE = uuid.UUID("2bb65330-cdce-40b7-a780-cd1fa3700fbd")
