"""Settings both services must agree on, so the models cannot drift apart."""

import uuid

DENSE_MODEL = "gemini-embedding-2"  # called over the Gemini API
SPARSE_MODEL = "Qdrant/bm25"
DENSE_DIM = 768

# The model reads at most 8192 tokens, and every token sent counts against the
# API limits, so longer texts are cut here. Document chunks are far shorter.
DENSE_MAX_CHARS = 8000

# Vectors of different models cannot be compared, even when they have the same
# size. Changing DENSE_MODEL means new names here, and re-embedding the old data
# (see retriever/reembed_collections.py).
MESSAGES_COLLECTION = "chat_messages_v3"
CHUNKS_COLLECTION = "document_chunks_v3"

# Used to build document and chunk ids (uuid5). NEVER change it: every stored
# id would change and old documents could no longer be found or replaced.
DOC_NAMESPACE = uuid.UUID("2bb65330-cdce-40b7-a780-cd1fa3700fbd")
