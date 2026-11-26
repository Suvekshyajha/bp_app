"""Settings both services must agree on, so the models cannot drift apart."""

import uuid

DENSE_MODEL = "ibm-granite/granite-embedding-small-english-r2"
SPARSE_MODEL = "Qdrant/bm25"
DENSE_DIM = 384

# The model can read 8192 tokens, but one 6000-token text takes about 5 seconds
# on a CPU, so longer texts are cut here. Document chunks are far shorter.
DENSE_MAX_TOKENS = 2048

# Vectors of different models cannot be compared, even when they have the same
# size. Changing DENSE_MODEL means new names here, and re-embedding the old data
# (see retriever/reembed_collections.py).
MESSAGES_COLLECTION = "chat_messages_v2"
CHUNKS_COLLECTION = "document_chunks_v2"

# Used to build document and chunk ids (uuid5). NEVER change it: every stored
# id would change and old documents could no longer be found or replaced.
DOC_NAMESPACE = uuid.UUID("2bb65330-cdce-40b7-a780-cd1fa3700fbd")
