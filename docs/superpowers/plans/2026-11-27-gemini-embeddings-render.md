# Gemini Embeddings and Render Deployment Implementation Plan

> **For agentic workers:** executed inline (superpowers:executing-plans). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the local Granite model with Gemini embeddings and make the backend deployable as one Render service, with the frontend on Vercel.

**Architecture:** `common/embeddings.py` calls the Gemini REST API with `httpx`; BM25 stays local. One Docker image runs the orchestrator (public, `$PORT`), retriever, ingestion API and worker (localhost only). Qdrant is Qdrant Cloud.

**Tech Stack:** Python 3.12, httpx 0.28.1, fastembed (BM25 only), Docker, Render Blueprint.

## Global Constraints

- Model `gemini-embedding-2`, 768 numbers per vector.
- The only embedding setting a person adds to `.env` is `GEMINI_API_KEY`; everything else has a default in code.
- Never open or edit a real `.env` file.
- No tests are written or run (the owner tests). Syntax compile checks only.
- No commits unless asked.
- Error messages and logs never contain document or chat text, or the API key.

## Review Focus

1. Gemini rate limit (429) during a document upload: wait and retry, do not fail at once.
2. Gemini slow or down during chat: the chat still answers without search, quickly.
3. Similarity thresholds are on a new scale and untuned.
4. Four processes may not fit in 512 MB.
5. Free Render loses SQLite files on restart while Qdrant Cloud keeps its points.

---

### Task 1: Gemini embeddings

**Files:** `backend/shared_common/common/settings.py`, `backend/shared_common/common/embeddings.py`

**Produces:** `require_api_key()`, `get_dense()`, `get_sparse()`, `warm_up()`, `embed_messages(list[str])`, `embed_passages(list[str])`, `embed_query(str)`, `count_tokens(str) -> int`, `EmbeddingError`. Result shape unchanged.

- [x] Settings: model, `DENSE_DIM = 768`, `DENSE_MAX_CHARS = 8000`, collections `*_v3`.
- [x] Query text `task: search result | query: {text}`; stored text `title: none | text: {text}`.
- [x] Batches of 100; `batchEmbedContents` items with `model`, `content`, `outputDimensionality`; header `x-goog-api-key`; normalize; reject wrong vector size.
- [x] Retry 429/5xx/network errors: query 2 attempts, message 3 (short waits), passages 7 (waits up to 60 s).
- [x] `count_tokens` = sum over words of `ceil(len(word) / 4)` (local estimate, additive).

### Task 2: Services follow the new embeddings

**Files:** `retriever/retrieval.py`, `retriever/doc_retrieval.py`, `retriever/reembed_collections.py`, `ingestion/worker.py`, `ingestion/chunking/tokens.py`, both `requirements.txt`

- [x] Default thresholds 0.5 and 0.2 (provisional).
- [x] Re-embed from `*_v2`, else the first collections.
- [x] Remove torch settings and torch-only packages.

### Task 3: Docker, Render, Codespaces

**Files:** `backend/render/Dockerfile`, `backend/render/start.sh`, `render.yaml`, retriever and ingestion Dockerfiles, `deploy/codespaces/*`

- [x] Start script makes the two service tokens if unset; stops the container if any process ends.
- [x] Blueprint asks only for the secrets and Qdrant address.

### Task 4: Examples and documents

**Files:** the retriever and ingestion `.env.example`, `README.md`, `docs/PROJECT_GUIDE.md`

- [x] Add `GEMINI_API_KEY`; describe deployment and the changed behaviour.
