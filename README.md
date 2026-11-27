# INFO MATE

A chat app with a memory. You talk to an AI assistant, and it can remember what you said earlier in the conversation. When you ask a question, the app searches your older messages for anything relevant and gives it to the AI along with your question. Everything that is saved is **encrypted**, so a copy of the database or of Qdrant's storage is unreadable without the server's key.

> **Status: work in progress.** The chat pipeline, sign up / login, saved chat history, rate limiting, encryption at rest, document upload with questions about your documents, service-to-service tokens (with optional TLS), request-traced logging, continuous integration and automated tests are built.

## Features

- **Sign up and login:** create an account with a name, email and password. Passwords are stored hashed (argon2), never as plain text.

- **Cookie-based sessions:** login uses an HttpOnly session cookie. A session lasts 12 minutes and restarts every time you send a message, so only idle users are logged out.

- **Secure chat connection:** the chat WebSocket checks both the website that opened it (Origin) and your session cookie. A bad or expired session closes the connection and logs you out automatically.

- **Chat with an AI:** your messages are answered by an AI model (Groq, `openai/gpt-oss-120b`).

- **Live answers:** the answer is streamed token by token, and you can stop it halfway (the part already written is kept). Up to 3 different chats can answer at the same time.

- **Edit and regenerate:** edit a question you already sent, or ask for a new answer to it. Sending the same message id again returns the saved answer instead of asking the model a second time.

- **Memory inside a chat:** the app searches the older messages of your chat by meaning and keywords and gives the relevant ones to the AI, on top of the recent history.

- **Private memory:** search is filtered by your user id and the chat id, so you only ever get your own messages.

- **Encrypted at rest:** message text and chat titles are encrypted before they are written to SQLite, and the message text stored in Qdrant is encrypted too. Only the retriever holds the key (see [Encryption at rest](#encryption-at-rest)).

- **Saved chat history:** conversations and messages are stored by the retriever in its own SQLite database and loaded in pages, so long histories stay fast. Users and sessions live in the orchestrator's SQLite database.

- **Search failures do not break chat:** if Qdrant is down, the answer is still written from the chat history, just without the extra related messages.

- **Per-user rate limit:** each user may send 10 messages per 60 seconds (across all their tabs). Over the limit, the app shows a countdown and pauses sending.

- **Connection heartbeat:** the browser pings the server every 25 seconds. A dead connection is detected within about 10 seconds and the app reconnects automatically.

- **Live chat screen:** typing indicator, connection status, automatic reconnect, retry for failed messages, and the related past messages shown as context under each answer.

- **Ask about your documents:** upload a PDF, DOCX, TXT or Markdown file (up to 10 MB), wait until it shows **Ready**, tick it, and ask questions about it. Up to 5 documents can be ticked per question, and the answer is written from the best matching parts of them. If you tick nothing, the app searches all your ready documents instead (with a stricter relevance check) and tells the AI which documents you have.

- **Short documents are never missed:** a document (or a selection of documents) that is only 4 parts long or less is given to the AI in full, so a short file is not dropped by the relevance check.

- **Sources under each answer:** the **Sources** panel lists the document parts (file name and part number) and the older chat messages the answer used. Sources are saved with the answer, so they are still there when the chat is loaded again.

- **Notifications:** a small message (toast) appears when an uploaded document finishes processing or fails. A failure stays on screen longer.

- **A separate ingestion service:** reading, splitting, embedding and storing a document is done by its own service and a background worker, so uploads do not slow down the chat. Document text and file names are encrypted, and every document belongs to one user.

- **Services must prove who they are:** the orchestrator sends a shared token to the retriever and to the ingestion service, and both refuse to start without one. TLS (and mutual TLS for the retriever) can be switched on with a few settings (see [Securing the service links](#securing-the-service-links)).

- **Traceable logs:** every log line shows the service and a request id that follows a question from the orchestrator into the retriever (see [Logging](#logging)).

- **Continuous integration:** GitHub Actions lints, tests and builds the frontend and runs the orchestrator and retriever tests on every pull request (see [Tests](#tests)).

- **One command start (Windows):** `run-all.ps1` starts Qdrant, the retriever, the ingestion API, the ingestion worker, the orchestrator and the frontend.

- **Automated tests:** orchestrator, retriever, ingestion, shared-package and frontend suites; each one prints its own test count (see [Tests](#tests)).

## How it works (in plain words)

1. You sign up or log in. The backend gives your browser a **session cookie** that proves who you are.

2. Your chats and their latest messages are loaded from the server (older ones load when you ask for them).

3. You type a message. It travels over a WebSocket to the **orchestrator**, which checks your session and the rate limit.

4. The orchestrator passes the question to the **retriever**, which searches the **older messages of this chat** for ones related to what you just asked.

5. Your question, the recent history and those related messages are sent to an AI model.

6. The AI's reply is streamed back as it is written.

7. Both your message and the AI's reply are encrypted and saved, so they can be found next time.

"Related" means related by **meaning**, not just matching words. A question about your "temperature" can find an old message that said "I had a fever".

## Architecture

The **retriever** (folder `backend/retriever`, also called the chat service) is the part that owns the conversation: it has its own SQLite database, calls the LLM, searches Qdrant and holds the encryption key. The orchestrator never touches any of those.

```text
                           ┌──────────────────────┐
                           │       Browser        │
                           │  React + TypeScript  │
                           └──────────────────────┘
                                       │  REST + WebSocket (session cookie)
                                       ▼
                  ┌────────────────────────────────────────┐
                  │              Orchestrator              │
                  │     login · sessions · rate limit      │
                  │                                        │
                  │     ┌────────────────────────────┐     │
                  │     │ SQLite chat.db             │     │
                  │     │ users and sessions only    │     │
                  │     └────────────────────────────┘     │
                  └────────────────────────────────────────┘
                                       │  gRPC + service token (optional TLS),
                                       │  internal network only
                                       ▼
      ┌───────────────────────────────────────────────────────────┐
      │  Retriever  (chat service, folder backend/retriever)      │
      │  one chat turn · history · search · encryption            │
      │                                                           │
      │  ┌─────────────────────────────────────────────────────┐  │
      │  │ Encryption (crypto.py): the key lives only here     │  │
      │  └─────────────────────────────────────────────────────┘  │
      │                                                           │
      │  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐  │
      │  │ SQLite        │  │ LLM client    │  │ Search        │  │
      │  │ chat.db       │  │ llm.py        │  │ embeddings +  │  │
      │  │ chats and     │  │ streams the   │  │ hybrid        │  │
      │  │ messages      │  │ answer        │  │ retrieval     │  │
      │  │ (encrypted)   │  │               │  │               │  │
      │  └───────────────┘  └───────┬───────┘  └───────┬───────┘  │
      └─────────────────────────────┬──────────────────┬──────────┘
                                    │ HTTPS            │ database client
                                    ▼                  ▼
                              ┌───────────┐   ┌─────────────────┐
                              │  Groq LLM │   │      Qdrant     │
                              │ (AI model)│   │  vectors + text │
                              └───────────┘   │ (text encrypted)│
                                              └─────────────────┘
```

### Documents and the ingestion service

Uploaded documents are handled by a separate **ingestion service** (folder `backend/ingestion`). It has an **API** process (accepts the file, checks it, answers at once) and a **worker** process (reads, splits, embeds, encrypts and stores the document). The retriever only **reads** the stored document chunks, to answer questions. Qdrant is the only link between the two; the retriever and the ingestion service never call each other.

```text
Browser (React) ──REST + WebSocket (session cookie)──► Orchestrator
                    login, sessions, rate limits, user_id from SESSION only,
                    upload checks (size, type), no document storage

  ① chat + user_id + document_ids (gRPC, existing channel) ──► Retriever
  ② upload / list / status / delete + user_id (HTTP, token) ──► Ingestion API

Retriever  (chat service, owns chat.db)
  history · memory search · DOCUMENT search (read only) · LLM · answer + sources
  ③ HTTPS ──► Groq
  ④ READ, filtered by user_id (+ document_id, status=ready for answers) ──► Qdrant

Ingestion service (owns ingest.db: one `documents` table, job state = its status and stage)
  API process:    accepts the file, checks it, writes a job row, returns at once
  Worker process: parse ► chunk ► embed ► encrypt ► upsert ► set status=ready LAST
  ⑤ WRITE (upsert, set_payload, delete) ──► Qdrant

Qdrant
  chat_messages_v3     retriever reads and writes
  document_chunks_v3   ingestion writes, retriever reads only
```

- **Upload:** Browser ► Orchestrator (checks, user id) ► Ingestion API (`pending`) ► Worker (`processing`) ► Qdrant ► `ready`. The browser lists its documents every 2 seconds while any is `pending` or `processing`, and shows a toast when one finishes or fails.

- **Ask:** Browser ► Orchestrator (user id, `document_ids`) ► Retriever ► owner and ready check ► filtered Qdrant search plus chat memory ► Groq ► streamed answer with sources. With no `document_ids`, the search covers all the user's ready documents.

- **Delete:** Browser ► Orchestrator ► Ingestion ► chunks removed from Qdrant, then the row from `ingest.db`.

- **Status:** Browser ► Orchestrator ► Ingestion (`ingest.db`).

New files for this feature: `orchestrator/documents.py` and `ingest_client.py`, `retriever/doc_retrieval.py`, `frontend/src/components/DocumentPanel.tsx`, `toasts.tsx` and `hooks/useDocuments.ts`. The shared code (encryption, service token, text cleaning, embeddings, logging) lives in `backend/shared_common` (package `common`).

### What each part does

| Part | Job |
|---|---|
| **Frontend** | The login and chat screens. Loads chats and messages from the REST API, sends your message, shows the streamed reply, and keeps the connection alive with a heartbeat. The server is the source of truth: the browser only remembers which chat was open and which chats you removed from your list. The login itself is an HttpOnly cookie that JavaScript cannot read. |
| **Orchestrator** | The front door. Handles sign up, login and sessions, checks the WebSocket's origin and cookie, applies the rate limit, and sends each question to the retriever. It relays what comes back (related messages, tokens, the final answer) to the browser and serves paginated history by asking the retriever. It no longer saves messages or calls the AI itself. |
| **SQLite, orchestrator** (`chat.db`) | Stores users and sessions only. Lives inside `backend/orchestrator`. |
| **Retriever** (chat service, folder `backend/retriever`) | The part that owns the conversation. For each question it checks that the chat belongs to the user, loads the recent history, searches related older messages, calls the AI, streams the answer, and saves both messages. It also converts messages into searchable vectors and is the only service that talks to Groq and the message database (the ingestion service also writes document chunks to Qdrant; the retriever only reads them). It holds the encryption key. |
| **SQLite, retriever** (`chat.db`) | Stores conversations and messages (text and titles encrypted). Lives inside `backend/retriever`. |
| **Qdrant** | The database that stores the message vectors and supports similarity search. The message text kept next to each vector is encrypted. |
| **Groq LLM** | The AI model (`openai/gpt-oss-120b`) that writes the reply. It is called only by the retriever. |

### Rules the design follows

- Frontend ↔ Orchestrator: REST and WebSocket. Both need a valid session cookie, and the WebSocket also checks the Origin header against `CORS_ORIGINS`.

- Orchestrator ↔ Retriever: gRPC (the contract is in `backend/proto/chat.proto`). A question is **one server-streaming call**; history is two plain request/response calls. Every call carries the shared `CHAT_SERVICE_TOKEN`, and the channel can use TLS.

- Retriever ↔ Groq: HTTPS, with the answer streamed back token by token.

- Retriever ↔ Qdrant: database connection. Chat messages are read and written; document chunks are **read only** (the retriever has no write path to them).

- Orchestrator ↔ Ingestion: HTTP with a shared service token (`INGEST_SERVICE_TOKEN`) and an `X-User-Id` header. The user id is the session's, never the browser's, and the ingestion service trusts it, so **keep port 8100 on an internal network** (or use TLS).

- Ingestion ↔ Qdrant: writes the document chunks (`document_chunks_v3`). The retriever and the ingestion service never call each other.

- A document id that does not exist and one that belongs to another user give the same "not found" answer.

- The orchestrator never talks to Qdrant, Groq or the message database directly.

- The user's id comes from the session, never from what the browser sends. The orchestrator passes it to the retriever, which trusts it, so **keep gRPC port 50051 on an internal network** and never expose it publicly. The token and TLS are extra layers, not a replacement for that.

- Only the retriever can read the stored messages: the encryption key is in its environment and nowhere else.

### REST API

All routes are under `/api`. Everything except `health`, `signup` and `login` needs the session cookie (otherwise `401`).

| Route | Purpose |
|---|---|
| `GET /health` | Health check |
| `POST /signup`, `POST /login` | Create an account / log in (sets the `session_id` cookie) |
| `POST /logout` | Deletes the session on the server |
| `GET /me` | Who is logged in |
| `GET /conversations?limit=&cursor=` | The user's chats, most recently active first |
| `GET /conversations/{id}/messages?limit=&cursor=` | One chat's messages, newest page first |
| `POST /documents` | Upload one document (multipart field `file`; `.pdf`, `.docx`, `.txt`, `.md`; up to `MAX_UPLOAD_MB`). `202` for a new document, `200` if the same file was uploaded before |
| `GET /documents` | The user's documents with their status |
| `GET /documents/{id}` | One document's status |
| `DELETE /documents/{id}` | Delete the document and its chunks (`204`) |

The two history routes are answered by the retriever; the orchestrator checks the session and forwards the request.

Each assistant message in the messages route also has `results`: the sources (document parts and older messages) that answer was written with, in the same shape as the live `done` message.

**Documents.** The document routes are answered by the ingestion service. The orchestrator first checks the session, the size, the type and an upload rate limit (default 5 per 60 seconds, otherwise `429`). A document id that does not exist and one that belongs to someone else both return `404`. Other answers: `409` (too many documents), `413` (too large), `415` (wrong type or empty), `502` or `503` (the document service is down or refused the request). A document goes `pending`, then `processing`, then `ready`, or ends as `failed` with a short error code.

**Pagination.** Both history routes use cursors, so new messages never shift the pages:

- Reply shape: `{"items": [...], "next_cursor": "..."}`. `next_cursor` is `null` on the last page.

- Pass `next_cursor` back as `cursor` to get the next page. For messages it leads to **older** messages; items inside a page are always oldest first.

- Defaults: 20 chats and 30 messages per page. `limit` must be 1–100 (otherwise `422`). A bad cursor returns `400`.

- A chat that belongs to someone else returns `404`, the same as a chat that does not exist.

- If the retriever is down or too slow, history routes return `503` or `504`.

### How login works

- `POST /api/signup` and `POST /api/login` create a session and set it in an **HttpOnly cookie** named `session_id`. No token is returned in the response body. Passwords are stored hashed (argon2).

- The cookie holds a random value. Only its SHA-256 hash is stored in the `sessions` table, so a leaked database cannot be used to log in.

- A session lasts `SESSION_MINUTES` (default **12**) and restarts every time you send a chat message. The cookie itself lives 7 days, but the real limit is the database.

- The frontend connects to `/ws/chat` (no token in the URL; the browser sends the cookie by itself).

- Cookies are sent between the page and the API only if both use the same host. Open the app as `localhost` **or** `127.0.0.1`, not a mix of both.

### WebSocket messages

Messages are JSON.

| Direction | Message | Meaning |
|---|---|---|
| Browser → server | `{"query": "...", "conversation_id": "..."}` | Ask a question |
| Browser → server | `{"query": "...", "conversation_id": "...", "document_ids": ["<uuid>", "..."]}` | Ask a question and also search these documents (at most 5, all must be `ready`) |
| Browser → server | `{"query": "...", "conversation_id": "...", "user_message_id": "<uuid>", "regenerate": true}` | Edit or regenerate: `user_message_id` is the id the browser gave the question. If the chat already has it, that message and everything after it are replaced. `regenerate` asks for a new answer to the same question |
| Browser → server | `{"type": "cancel", "conversation_id": "..."}` | Stop the answer being written in that chat |
| Browser → server | `{"type": "ping"}` | Heartbeat |
| Server → browser | `{"type": "pong"}` | Heartbeat reply |
| Server → browser | `{"type": "token", "content": "...", "conversation_id": "..."}` | One piece of the answer |
| Server → browser | `{"type": "done", "answer": "...", "results": [...], "cancelled": false, "conversation_id": "..."}` | The answer is complete (`cancelled: true` if you stopped it; `answer` then holds what was written so far) |
| Server → browser | `{"error": "...", "conversation_id": "..."}` | Something went wrong |
| Server → browser | `{"error": "...", "conversation_id": "...", "retry_after": 7}` | Rate limit reached; wait `retry_after` seconds |

**Close codes**

| Code | Sent by | Meaning | Frontend reaction |
|---|---|---|---|
| `4401` | server | No valid session (missing, expired, logged out) | Log the user out |
| `4403` | server | Website is not in `CORS_ORIGINS` | Stop retrying |
| `4001` | browser | No pong within 10 seconds | Reconnect with backoff |

**Rules**

- **Documents:** `document_ids` is optional. Leave it out and all the user's ready documents are searched. A bad list gives `Invalid document id.`. A document that is still being processed gives `A selected document is still being processed. Try again in a moment.` and nothing is saved. A missing document, or one that belongs to someone else, gives `A selected document was not found.` In the `results` of an answer, a document part has `document_id`, `filename` and `chunk_index` (and sometimes `section_title` and `page`); an older chat message has `role` and `timestamp` instead.

- One answer at a time **per chat**, and at most 3 chats answering at once. Extra messages are refused with an error. The server keeps reading while it answers, which is how `cancel` gets through. Cancelling closes the gRPC call, and the retriever saves the part of the answer written so far.

- **Heartbeat:** a ping checks that the session is still valid (an expired login is closed with `4401`), but it does **not** extend the session and does **not** count toward the rate limit. Only real chat messages count as activity.

- **Rate limit:** a sliding window per user (default 10 messages per 60 seconds), checked before any database or AI work. Blocked attempts are not counted. The counters live in the server's memory, so they reset on restart and each worker counts separately. For several workers or servers, move the counters to Redis.

### How the retriever link works (gRPC)

The retriever runs two gRPC services on port 50051 (`backend/proto/chat.proto`):

| Service | Call | Kind |
|---|---|---|
| `ChatService` | `Chat` | One request, a **stream of events** back |
| `HistoryService` | `ListConversations`, `ListMessages` | Plain request / response |

**One question = one call.** The orchestrator sends `user_id`, `conversation_id` and `query` (and, when relevant, `document_ids`, `user_message_id` and `regenerate`), and receives events in this order:

1. `Context`: once, after the search, before the first token (the related older messages and document excerpts).

2. `Token`: many, one per piece of the answer.

3. `Done`: once, last (ids of the saved messages and the full answer).

There is no shared stream between users any more, so a slow answer never holds up another. The stop button cancels the call. Every call carries the service token and a request id as gRPC metadata.

**Inside the retriever** (`turn.py`), each question runs these steps:

1. Check the chat exists and belongs to this user (it is created on the first message).

2. Load the latest messages of the chat as history, before the new question is saved.

3. Search related older messages. Ones already in the history are dropped so nothing is sent twice.

4. Search the documents: the ones the user chose, or all their ready documents if none were chosen (see [Searching documents](#searching-documents)). If none were chosen, the model is also told the names of the user's documents.

5. Save the question, then index it in Qdrant in the background. It is saved **after** the search, so a question cannot find itself.

6. Stream the answer from Groq.

7. Save the answer, then index it in the background.

**What this means in practice**

- If the search fails (for example Qdrant is down), the answer is written from the chat history alone and the error is logged. A failing document search does the same: the chat still answers.

- `done` is sent as soon as the answer is saved in SQLite. Indexing in Qdrant may still be running, but the next question already sees the answer through the history.

- If the model call fails, nothing half-written is saved. If you press stop (or close the browser), the partial answer is saved.

- Embedding and search work is capped at 4 jobs at a time.

**Errors** become gRPC status codes, which the orchestrator turns into WebSocket errors or HTTP codes: `NOT_FOUND` (chat of someone else, shown as `404`), `INVALID_ARGUMENT` (bad cursor, `400`), `RESOURCE_EXHAUSTED` (model busy), `DEADLINE_EXCEEDED` (model too slow, `504`), `UNAVAILABLE` (model or service unreachable, `503`) and `INTERNAL`.

### How search works

Each saved message is turned into two kinds of vectors:

- **Dense** (`gemini-embedding-2` over the Google Gemini API, 768 numbers): captures **meaning**. The text is sent to Google readable. On the free tier Google may use it to improve its products and people may review it, so use a paid (billing enabled) key for real personal or sensitive data.

- **Sparse** (`Qdrant/bm25`): captures **keywords**.

A search runs both and merges the two ranked lists with **Reciprocal Rank Fusion (RRF)**. This is called hybrid search. Results are filtered by `user_id` and `conversation_id`, so a user only searches their own messages in that chat. Matches whose dense similarity is below a threshold (default 0.5, set with `RETRIEVAL_MIN_SIMILARITY`) are dropped, so unrelated messages are kept away from the AI. The default has not been tuned for this model yet: compare the `Retrieval:` log lines with real questions and adjust it.

The vectors are always made from the plain text, **before** the text is encrypted for storage.

#### Searching documents

Document chunks are searched the same hybrid way, but always filtered by the user id, the document ids (when chosen) and `status = ready`. What differs is how strict the relevance check is:

| Situation | What the AI gets |
|---|---|
| **Documents ticked, 4 parts or less in total** | All the parts, in reading order. There is nothing to rank, so no relevance check is applied (`RETRIEVAL_SMALL_SELECTION_CHUNKS`). |
| **Documents ticked, more parts** | The best 6 parts. Only parts that point away from the question (dense similarity below 0.2, `RETRIEVAL_SELECTED_MIN_SIMILARITY`) are dropped. The user chose the scope, so a weak match is still allowed. |
| **Nothing ticked** | The best parts of **all** the user's ready documents, with the normal 0.5 threshold, so a question no document covers finds nothing. The AI is also given the names of the documents (at most `MAX_AVAILABLE_DOCUMENTS`). |

On top of that:

- When several documents are searched, one document may fill at most 4 of the 6 places, so a question several documents touch is not answered from only one. Unused places are given back.

- The parts right before and after the 2 best hits are added as extra context, because an answer often runs over a part border. They come last, so they are the first to be cut when the prompt is full.

- The excerpts are cleaned so a document cannot close the prompt block it sits in, and the total size of the excerpts is capped (`DOC_BUDGET_CHARS`).

- The retriever never writes to the document collection.

## Encryption at rest

Messages and chat titles are encrypted by the application before they reach any storage, so someone who copies `chat.db`, a backup or Qdrant's files, but not the key, sees only ciphertext.

```text
  plain text ──► crypto.py (key from CHAT_ENCRYPTION_KEYS) ──► enc:v1:gAAAA...  ──► SQLite / Qdrant
  plain text ◄── crypto.py (same key, retriever only)   ◄── enc:v1:gAAAA...  ◄── SQLite / Qdrant
```

| Item | Detail |
|---|---|
| **Approach** | Application-level encryption. It works with plain SQLite (which has no built-in encryption) and covers Qdrant with the same code. Add disk encryption (for example BitLocker, or an encrypted cloud volume) underneath as a second layer. |
| **Cipher** | Fernet from the `cryptography` package: AES-128-CBC plus an HMAC, so changed or corrupted data is detected. |
| **Encrypted** | `messages.content` and `conversations.title` in SQLite, and the `content` field of each Qdrant payload. Document text and file names are encrypted the same way by the ingestion service. |
| **Not encrypted** | Ids, roles and timestamps (needed for queries), the search vectors themselves (needed for search), and the user names and emails in the orchestrator's database. |
| **Key** | `CHAT_ENCRYPTION_KEYS` in `backend/retriever/.env` (and the same value in `backend/ingestion/.env`). The orchestrator never has it. |
| **How it plugs in** | `crypto.py` provides an `EncryptedText` column type used in `models.py`, so `repository.py` keeps working with plain strings. `qdrant_store.py` encrypts the payload text and `retrieval.py` decrypts it. |
| **Old data** | Values without the `enc:v1:` prefix are treated as old plain text and still readable. `encrypt_existing.py` converts them. |

**Make a key** (once):

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Encrypt data you already have** (stop the services first, run from `backend/retriever`):

```powershell
python encrypt_existing.py --qdrant
```

It encrypts the SQLite rows and the Qdrant payloads, then runs a checkpoint and `VACUUM` so old plain text does not stay in the file. It is safe to run again.

**Upgrading from an earlier embedding model** (Granite or `bge-base-en-v1.5`): its vectors cannot be searched with the new model, so the new one uses new collections. Start Qdrant, stop the other services, then run once from `backend/retriever`:

```powershell
python reembed_collections.py
```

It copies the `_v2` collections (or, if there are none, `chat_messages` and `document_chunks`) into `chat_messages_v3` and `document_chunks_v3` with new vectors. The old collections are only read. Delete them once you are happy with the results, because they still hold the old (encrypted) text. Do not run the script again after using the app, or anything deleted since is copied back. If your `.env` sets `RETRIEVAL_MIN_SIMILARITY`, remove that line so the new default applies.

**Key rules**

- **Back the key up** somewhere safe and separate. If it is lost, the stored chats cannot be recovered.

- **Never commit it.** Keep `.env` out of git. In production, load it from a secrets manager.

- **Rotation:** put the new key first and keep the old one after it, comma separated: `CHAT_ENCRYPTION_KEYS=newkey,oldkey`. New data uses the first key; all keys can read.

- A wrong or missing key stops the service with a clear error instead of returning garbage.

- This protects data **at rest**. Someone who controls the running server and its `.env` can still read the chats.

## Securing the service links

The retriever and the ingestion service both trust the user id they are given, so they must be sure the caller is the orchestrator.

**Service tokens (required).** Each pair shares one secret:

| Token | Set in | Used for |
|---|---|---|
| `CHAT_SERVICE_TOKEN` | the retriever **and** the orchestrator | gRPC calls to the retriever |
| `INGEST_SERVICE_TOKEN` | the ingestion service **and** the orchestrator | HTTP calls to the ingestion API |

Make each one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. A service refuses to start without its token, and refuses an example value such as `change_me`. A token shorter than 16 characters only gives a warning. For local development only, `CHAT_ALLOW_NO_AUTH=true` or `INGEST_ALLOW_NO_AUTH=true` lets a service start without a token, and only while it listens on this machine (`CHAT_BIND_HOST` / `INGEST_BIND_HOST` left at `127.0.0.1`). The orchestrator also stops at start-up if `INGEST_SERVICE_TOKEN` is missing.

**TLS for the chat service (optional).** For a quick local test, run `python make_dev_certs.py` in `backend/orchestrator`. It writes a self-signed certificate valid for one year for `localhost` and `127.0.0.1` into `certs/` (never commit `certs/*.key`). Then set:

| File | Setting |
|---|---|
| `backend/retriever/.env` | `CHAT_TLS_CERT=certs/server.crt` and `CHAT_TLS_KEY=certs/server.key` (paths to the files) |
| `backend/orchestrator/.env` | `CHAT_TLS_CA=certs/server.crt` |

For **mutual TLS**, also set `CHAT_TLS_CLIENT_CA` in the retriever, and `CHAT_TLS_CLIENT_CERT` plus `CHAT_TLS_CLIENT_KEY` in the orchestrator. Setting only one of the client certificate and key stops the orchestrator on purpose, because half a pair would silently turn mutual TLS off. In a real deployment use a certificate from your own CA or your hosting provider.

**Ingestion API.** The ingestion API does not serve TLS itself. `INGEST_TLS_CA` in the orchestrator is the certificate that signed the ingestion service's certificate, for when it sits behind something that does (for example a reverse proxy). Otherwise keep port 8100 on an internal network.

**Qdrant.** `docker-compose.yml` reads `QDRANT_API_KEY` from a `.env` file next to it (not committed). Use the same value as `QDRANT_API_KEY` in the retriever's and the ingestion service's `.env`. The key is sent with every request, so over plain `http` (fine on localhost) it is readable on the network: use `https` for a real deployment. The Qdrant client warns about this ("Api key is used with an insecure connection").

## Deploying (Vercel + Render)

The frontend runs on Vercel. The whole backend runs as **one** Render web service (`backend/render/Dockerfile`): the orchestrator is public, and the retriever, ingestion API and worker listen only inside the container. The search index is Qdrant Cloud.

1. Create a cluster on Qdrant Cloud and note its `https://...` address and API key.
2. Push the code to GitHub. In Render choose **New > Blueprint** and pick the repository and branch (`render.yaml`).
3. Enter `GEMINI_API_KEY`, `GROQ_API_KEY`, `QDRANT_URL` and `QDRANT_API_KEY`. The service tokens are made at every start, and `CHAT_ENCRYPTION_KEYS` is made once by Render: copy it somewhere safe.
4. If your Vercel address is not the one in `render.yaml`, change `CORS_ORIGINS`.
5. In Vercel set `VITE_API_BASE_URL=https://<service>.onrender.com` and `VITE_WS_URL=wss://<service>.onrender.com/ws/chat`, then redeploy.

Limits of the free plans:

- Render free has no disk: **accounts, chats and the document list are lost at every restart or sleep**, while their points stay in Qdrant, where nothing can reach them. For lasting data use a paid plan with a disk mounted at `/data` (the disk must be writable by the container's `appuser`).
- Render free has 512 MB of memory. Whether the four processes fit has not been measured. If Render reports out of memory, use a 2 GB plan.
- A free Qdrant cluster is suspended when unused for a week.

## Logging

The orchestrator and the retriever write every line as `time LEVEL [service] [request-id] logger: message`.

- **Request id:** the orchestrator makes one per HTTP request and per WebSocket connection (or takes the incoming `x-request-id` header). It sends it to the retriever as gRPC metadata and to the ingestion API as `X-Request-ID`, so one question can be followed across services by searching for its id.

- **Level:** set `LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, `ERROR`) in the orchestrator's or the retriever's `.env`. The default is `INFO`.

- **What is logged:** ids, file names, sizes and counts, never message text or file contents. For uploads, refusals (wrong type, empty, too large, a file whose content does not match its type, document limit) are warnings, accepted and duplicate uploads and deletes are `INFO`, and the step-by-step lines (for example "File signature check passed for ...") are `DEBUG`.

- **Ingestion service:** it sets its own level in code (`INFO`) and does not read `LOG_LEVEL` yet, so its `DEBUG` lines only appear if you change that level in `backend/ingestion/main.py` (and `worker.py` for the worker).

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | React, TypeScript, Vite |
| API | Python, FastAPI (REST + WebSocket) |
| Login | Server-side sessions with an HttpOnly cookie, password hashing (pwdlib + argon2) |
| Chat history | SQLite, SQLAlchemy, cursor pagination (in the retriever) |
| Encryption | `cryptography` (Fernet), application-level, for messages, titles, document text and Qdrant payload text |
| Service-to-service | gRPC (`grpc.aio`, server streaming for chat), Protocol Buffers; shared service tokens, optional TLS |
| Embeddings | Google Gemini API (dense), FastEmbed (sparse) |
| Vector database | Qdrant |
| AI model | Groq API, `openai/gpt-oss-120b` (streamed) |
| Tests | pytest (backend), Vitest + Testing Library (frontend), ESLint |
| CI | GitHub Actions |
| Containers | Docker (Qdrant locally; `backend/render/Dockerfile` for deployment) |

## Folder structure

```text
day2-bp_app/
├── .github/workflows/ci.yml  # CI: frontend lint, test, build; backend tests
├── docs/                     # design notes (document ingestion migration plan)
├── frontend/                 # React app
│   ├── vite.config.ts        # Vite + Vitest config
│   └── src/
│       ├── api.ts            # REST calls (+ their tests: api.test.ts)
│       ├── types.ts
│       ├── services/         # chatSocket.ts (WebSocket + heartbeat), storage, identity, documentLimits
│       ├── hooks/            # useChatSocket, useConversations, useChatMessages, useDocuments (upload, list, delete, polling, notices)
│       ├── components/       # Sidebar, MessageList, DocumentPanel, ContextPanel (Sources), toasts (notifications), ...
│       └── pages/            # LoginPage, ChatPage
│                             # *.test.ts files sit next to the code they test
├── backend/
│   ├── orchestrator/         # FastAPI: login, sessions, rate limit, REST + WebSocket relay
│   │   ├── .env              # your settings (not in git)
│   │   ├── .env.example
│   │   ├── main.py           # FastAPI app: CORS, /api routes, /ws/chat
│   │   ├── routes.py         # /api/health, /signup, /login, /logout, /me
│   │   ├── history.py        # /api/conversations and messages (forwards to the retriever)
│   │   ├── documents.py      # /api/documents (checks, upload rate limit, forwards to the ingestion service)
│   │   ├── websocket.py      # /ws/chat (cookie + origin check, heartbeat, rate limit, relay, cancel)
│   │   ├── rate_limit.py     # per-user sliding-window rate limiter
│   │   ├── auth.py           # password hashing, sessions, cookies
│   │   ├── database.py       # SQLite connection
│   │   ├── models.py         # users and sessions
│   │   ├── chat.db           # SQLite database: users and sessions (created automatically)
│   │   ├── chat_client.py    # gRPC client for the retriever (token, optional TLS)
│   │   ├── ingest_client.py  # HTTP client for the ingestion service (token, optional TLS CA)
│   │   ├── make_dev_certs.py # self-signed certificate for local TLS tests
│   │   ├── chat_pb2.py       # generated from chat.proto (not in git)
│   │   ├── chat_pb2_grpc.py  # generated from chat.proto (not in git)
│   │   ├── pytest.ini
│   │   ├── requirements.txt
│   │   ├── requirements-dev.txt    # adds pytest and httpx
│   │   └── tests/                  # conftest.py, test_rate_limit.py, test_history.py, test_Websocket.py, test_documents.py, test_ingest_client.py, test_chat_client_*.py
│   ├── retriever/            # the retriever (also called the chat service): turns, history, search, LLM, encryption
│   │   ├── .env              # QDRANT_URL, GROQ_*, CHAT_ENCRYPTION_KEYS, CHAT_SERVICE_TOKEN (not in git)
│   │   ├── .env.example
│   │   ├── main.py           # gRPC server: ChatService and HistoryService (token, optional TLS)
│   │   ├── turn.py           # one chat turn: search, save, ask the LLM, save the answer
│   │   ├── repository.py     # all SQLite reads and writes, cursor pagination
│   │   ├── database.py       # SQLite connection
│   │   ├── models.py         # conversations and messages (encrypted columns)
│   │   ├── chat.db           # SQLite database: conversations and messages (created automatically)
│   │   ├── crypto.py         # encryption: key loading and the EncryptedText column type
│   │   ├── encrypt_existing.py          # one-off: encrypt data stored as plain text
│   │   ├── migrate_from_orchestrator.py # one-off: copy old chats from the orchestrator's chat.db
│   │   ├── reembed_collections.py       # one-off: move vectors to a new embedding model
│   │   ├── llm.py            # Groq streaming, prompt building
│   │   ├── prompt_budget.py  # keeps the history inside the prompt size limit
│   │   ├── retrieval.py      # hybrid search of chat messages (decrypts the payload text)
│   │   ├── doc_retrieval.py  # read-only hybrid search of document chunks
│   │   ├── embeddings.py     # dense and sparse vectors
│   │   ├── qdrant_store.py   # Qdrant collection and saving (encrypts the payload text)
│   │   ├── chat_pb2.py       # generated from chat.proto (not in git)
│   │   ├── chat_pb2_grpc.py  # generated from chat.proto (not in git)
│   │   ├── test_retrieval.py # tests of the real search code
│   │   ├── pytest.ini
│   │   ├── requirements.txt
│   │   └── tests/            # conftest.py, test_turn.py, test_repository.py, test_grpc.py, test_doc_retrieval.py, ...
│   ├── ingestion/            # the ingestion service: API (main.py) and worker (worker.py)
│   │   ├── .env.example
│   │   ├── routes.py         # /documents: upload, list, status, delete
│   │   ├── jobs.py           # one document job: parse, analyse, chunk, embed, store
│   │   ├── worker.py         # background worker (runs at low priority)
│   │   ├── parsers.py, stats.py, analyzer.py, analysis_schema.py, chunking/   # reading and splitting
│   │   ├── qdrant_docs.py    # writes the encrypted chunks to Qdrant
│   │   ├── repository.py, models.py, database.py   # ingest.db: documents and job state
│   │   └── tests/
│   ├── shared_common/        # package `common`: crypto, service_auth, sanitize, embeddings, settings, log
│   ├── proto/
│   │   └── chat.proto        # the gRPC contract (the only copy)
│   └── requirements.txt
├── docker-compose.yml        # Qdrant
├── run-all.ps1               # starts everything on Windows
└── README.md
```

There are two `chat.db` files, and each is created automatically the first time its service starts: `backend/orchestrator/chat.db` (users and sessions) and `backend/retriever/chat.db` (conversations and messages, text encrypted). Both hold user data, so keep them out of git (if one is already tracked, run `git rm --cached backend/orchestrator/chat.db` or `git rm --cached backend/retriever/chat.db`).

## Setup

### Requirements

- Python 3.10+ (CI uses 3.12)

- Node.js 18+ (a recent version is recommended: CI uses 22)

- Docker Desktop

- A free [Groq API key](https://console.groq.com/)

### 1. Python environment (one venv for the backend)

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r orchestrator\requirements-dev.txt   # includes requirements.txt + test tools
pip install -r retriever\requirements.txt          # includes groq, SQLAlchemy and cryptography
pip install -e shared_common                       # the shared package `common`
pip install -r ingestion\requirements.txt          # parsers, chunking, Qdrant, Groq
```

### 2. Environment files

Two service tokens are needed before anything starts (see [Securing the service links](#securing-the-service-links)). Make them once:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"   # CHAT_SERVICE_TOKEN
python -c "import secrets; print(secrets.token_urlsafe(32))"   # INGEST_SERVICE_TOKEN
```

`backend/orchestrator/.env`

```env
CHAT_SERVICE_ADDRESS=localhost:50051
CHAT_SERVICE_TOKEN=the_same_token_as_in_the_retriever_env
SESSION_MINUTES=12
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
COOKIE_SECURE=false
COOKIE_SAMESITE=lax
CHAT_RATE_LIMIT_MESSAGES=10
CHAT_RATE_LIMIT_WINDOW_SECONDS=60
```

The settings other than the token have the defaults shown above. The orchestrator no longer needs any `GROQ_*` setting.

| Setting | What it does |
|---|---|
| `CHAT_SERVICE_ADDRESS` | Where the retriever listens. The older name `RETRIEVER_ADDRESS` still works if this one is not set. |
| `CHAT_SERVICE_TOKEN` | Required. The same value as in the retriever's `.env`. Sent with every call. |
| `CHAT_TLS_CA`, `CHAT_TLS_CLIENT_CERT`, `CHAT_TLS_CLIENT_KEY` | Optional TLS to the retriever. Leave empty on one machine. See [Securing the service links](#securing-the-service-links). |
| `SESSION_MINUTES` | How long an idle login lasts. It restarts on every chat message. |
| `CORS_ORIGINS` | Website addresses allowed to call the API and open the chat WebSocket. |
| `COOKIE_SECURE`, `COOKIE_SAMESITE` | Cookie settings. Local (http): `false` and `lax`. Deployed with the frontend and API on different sites: `true` and `none`. `none` requires `true`, or the orchestrator will not start. |
| `CHAT_RATE_LIMIT_MESSAGES`, `CHAT_RATE_LIMIT_WINDOW_SECONDS` | Each user may send this many messages in any window of this many seconds. |
| `LOG_LEVEL` | Optional. `INFO` by default. See [Logging](#logging). |

> `SECRET_KEY` is no longer used. Login used to be a signed JWT, but it is now a server-side session, so you can remove `SECRET_KEY` from your `.env` and `.env.example` (and `PyJWT` from `requirements.txt`).

`backend/retriever/.env`

```env
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=
GEMINI_API_KEY=your_key_here
GROQ_API_KEY=your_key_here
GROQ_MODEL=openai/gpt-oss-120b
CHAT_ENCRYPTION_KEYS=your_generated_key_here
CHAT_SERVICE_TOKEN=the_token_you_made_for_the_chat_service
```

`QDRANT_URL`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `CHAT_ENCRYPTION_KEYS` and `CHAT_SERVICE_TOKEN` are required. The service will not start without them.

| Setting | What it does |
|---|---|
| `GEMINI_API_KEY` | Required. Used to make the search vectors. The same key goes in the ingestion service. |
| `GROQ_API_KEY`, `GROQ_MODEL` | Moved here from the orchestrator. The retriever is the only part that calls the AI. |
| `CHAT_ENCRYPTION_KEYS` | The key that encrypts stored messages and titles. Generate it with the command in [Encryption at rest](#encryption-at-rest). Back it up; never commit it. |
| `CHAT_SERVICE_TOKEN` | Required, and the same value in the orchestrator. `CHAT_ALLOW_NO_AUTH=true` skips it for local development only. |
| `QDRANT_API_KEY` | Optional. Must match the key Qdrant was started with (see [Securing the service links](#securing-the-service-links)). |
| `CHAT_BIND_HOST`, `CHAT_SERVICE_PORT` | Where the gRPC server listens. Default `127.0.0.1` and `50051`. Use `0.0.0.0` only in Docker or on a server, and then also set the token and TLS. |
| `CHAT_TLS_CERT`, `CHAT_TLS_KEY`, `CHAT_TLS_CLIENT_CA` | Optional TLS. Set `CHAT_TLS_CERT` and `CHAT_TLS_KEY` together; `CHAT_TLS_CLIENT_CA` turns on mutual TLS. |
| `CHAT_DB_PATH` | Optional. Keeps the retriever's `chat.db` somewhere other than `backend/retriever`. |
| `RETRIEVAL_MIN_SIMILARITY` | Optional. Minimum similarity (0 to 1) for a related message, or a document part when no document is chosen, to be used. Default 0.5, not yet tuned: the scale belongs to the embedding model. |
| `RETRIEVAL_SELECTED_MIN_SIMILARITY` | Optional. The lower floor used when the user ticked documents. Default 0.2. It is never stricter than `RETRIEVAL_MIN_SIMILARITY`. Set it to 0.5 or more to switch the relaxed floor off. |
| `RETRIEVAL_SMALL_SELECTION_CHUNKS` | Optional. Ticked documents with this many parts or fewer are given to the AI whole. Default 4. |
| `PROMPT_BUDGET_CHARS` | Optional. Size limit of the whole prompt. Default 24000. |
| `DOC_BUDGET_CHARS` | Optional. How much document text goes into one prompt. Default 12000. |
| `MAX_AVAILABLE_DOCUMENTS` | Optional. Most document names told to the AI when no document is ticked. Default 50. |
| `MAX_QUERY_CHARS` | Optional. Longest question accepted. Default 4000. |
| `LOG_LEVEL` | Optional. `INFO` by default. |

Add these to `backend/orchestrator/.env` (the defaults are shown; the token is required, and the orchestrator will not start without it):

```env
INGEST_SERVICE_URL=http://localhost:8100
INGEST_SERVICE_TOKEN=the_same_token_as_in_the_ingestion_env
INGEST_TLS_CA=
MAX_UPLOAD_MB=10
DOC_UPLOAD_RATE_LIMIT=5
```

`backend/ingestion/.env` (copy `.env.example`)

```env
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=
CHAT_ENCRYPTION_KEYS=exactly_the_same_keys_as_the_retriever
INGEST_SERVICE_TOKEN=a_long_random_token
GEMINI_API_KEY=your_key_here
GROQ_API_KEY=your_key_here
```

The ingestion worker will not start without `GEMINI_API_KEY`, the ingestion service will not start without `INGEST_SERVICE_TOKEN`, and a different `CHAT_ENCRYPTION_KEYS` than the retriever's means document text cannot be read back.

| Setting | What it does |
|---|---|
| `INGEST_SERVICE_TOKEN` | Required, and the same value in the ingestion service and the orchestrator. |
| `CHAT_ENCRYPTION_KEYS` (ingestion) | The same keys as the retriever. Document text and file names are encrypted with them. |
| `QDRANT_URL`, `QDRANT_API_KEY` | The same Qdrant as the retriever. |
| `INGEST_BIND_HOST`, `INGEST_PORT` | Where the ingestion API listens. Default `127.0.0.1` and `8100`. |
| `GROQ_API_KEY`, `INGEST_ANALYSIS_MODEL`, `INGEST_ANALYSIS_ENABLED` | Stage 1 of chunking: an AI model picks how a document is split. Without a key, or with `INGEST_ANALYSIS_ENABLED=false`, fixed rules choose and the upload still works. |
| `MAX_UPLOAD_MB`, `MAX_DOCS_PER_USER` | 10 MB and 20 documents. Keep `MAX_UPLOAD_MB` equal in the orchestrator, the ingestion service and (as `VITE_MAX_UPLOAD_MB`) the frontend. |
| `INGEST_SEMANTIC_MAX_CHARS`, `INGEST_SEMANTIC_PERCENTILE`, `INGEST_JOB_TIMEOUT_SECONDS` | Tuning. Defaults 80000, 90 and 300. |
| `INGEST_DB_PATH`, `INGEST_UPLOAD_DIR` | Defaults `./ingest.db` and `./data/uploads`. |
| `INGEST_ALLOW_NO_AUTH` | Development only, localhost only: start without a token. |
| `INGEST_SERVICE_URL`, `INGEST_TLS_CA` (orchestrator) | Where and how the orchestrator reaches the ingestion API. |
| `DOC_UPLOAD_RATE_LIMIT` (orchestrator) | Uploads per user per 60 seconds. Default 5. |

Ports: Qdrant 6333, retriever 50051, orchestrator 8000, frontend 5173, **ingestion 8100**.

`.env` in the project root (next to `docker-compose.yml`, only needed if Qdrant should require an API key)

```env
QDRANT_API_KEY=the_same_key_as_in_the_retriever_and_ingestion_env
```

`frontend/.env`

```env
VITE_API_BASE_URL=http://localhost:8000
VITE_WS_URL=ws://localhost:8000/ws/chat
# VITE_MAX_UPLOAD_MB=10   (optional; keep equal to MAX_UPLOAD_MB in the orchestrator)
```

**Never commit `.env` files, either `chat.db`, the encryption key, the service tokens or TLS private keys.** The `.env` files and databases are listed in `.gitignore`. Only `.env.example` files belong in git.

### 3. Frontend packages

```powershell
cd frontend
npm install
```

If you add the tests to an existing checkout, also run `npm i -D vitest jsdom @testing-library/react` and add `"test": "vitest run"` to the `scripts` in `package.json`.

## Running the app

### Option A: one command (Windows)

From the project root:

```powershell
.\run-all.ps1
```

This starts Qdrant (Docker) and opens separate windows for the retriever, the ingestion API, the ingestion worker (at low priority), the orchestrator and the frontend. The first retriever start downloads the small keyword (BM25) model. Wait for `Chat service running on port 50051` before chatting.

### Option B: manually

```powershell
# 1. Qdrant
docker compose up -d

# 2. Retriever (in backend\retriever, venv active)
python main.py

# 3. Ingestion API (in backend\ingestion, venv active)
python main.py

# 4. Ingestion worker (in backend\ingestion, venv active, its own window)
python worker.py

# 5. Orchestrator (in backend\orchestrator, venv active)
uvicorn main:app --reload --port 8000

# 6. Frontend (in frontend)
npm run dev
```

Start the retriever, the ingestion API and the ingestion worker before the orchestrator. The worker is what turns an uploaded document from `pending` into `ready`. Then open **http://localhost:5173** and create an account. The frontend must run on an address listed in `CORS_ORIGINS` (by default port 5173) because the backend only allows those addresses.

### Quick checks

- API health: http://localhost:8000/api/health should return `{"status": "healthy"}`.

- Qdrant dashboard: http://localhost:6333/dashboard.

- Ingestion health: http://localhost:8100/health should return `{"status": "healthy"}`.

## Tests

The tests need no Qdrant, Groq key or Docker. The backend tests replace the retriever, Qdrant and the AI with fakes and use a temporary SQLite file per test, so the real `chat.db` files are never touched. The retriever tests use a throw-away encryption key.

```powershell
# Orchestrator: rate limiter, pagination, WebSocket relay, heartbeat, auth checks, documents (the runner prints the count)
cd backend\orchestrator
pytest

# Retriever: turns, repository, gRPC, encrypted columns, document search (the runner prints the count)
cd backend\retriever
pytest

# Real search code: run on its own, because the tests above replace this module with a fake
pytest test_retrieval.py

# Ingestion: parsers, chunking, Qdrant storage, jobs, worker, routes (fake embeddings and a fake Qdrant)
cd backend\ingestion
pytest

# Shared package (crypto, service token, sanitizer)
cd backend\shared_common
pytest

# Frontend: API calls, heartbeat, rate-limit handling, history hooks, documents (upload, polling, selecting, notices) and Sources (the runner prints the count)
cd frontend
npm test
npm run lint      # ESLint
npm run build     # type-checks with tsc, then builds
```

Useful variations:

```powershell
pytest tests/test_history.py          # one backend file
pytest -k rate                        # tests whose name contains "rate"
npx vitest                            # frontend watch mode (re-runs on save)
npx vitest run src/api.test.ts        # one frontend file
```

### Continuous integration

`.github/workflows/ci.yml` runs on every pull request and on every push to `dev` or `main`:

- **Frontend:** `npm ci`, `npm run lint`, `npm test`, `npm run build` (Node 22).

- **Backend, orchestrator and retriever:** installs `requirements-dev.txt`, generates the gRPC Python files from `backend/proto/chat.proto` (they are not stored in git), then runs `python -m pytest tests` (Python 3.12).

Some modules read a secret when they are imported (for example the Groq key, or `INGEST_SERVICE_TOKEN` in the orchestrator), and a real `.env` is never in git. The workflow's `Test` step therefore gives the tests dummy values. If CI fails at collection with a "... is missing" error, add the missing variable there. The ingestion and shared-package tests are not part of CI yet, so run them locally.

## Troubleshooting

**Chat sends me back to the login page**

- Your session is missing, wrong or expired (it ends after `SESSION_MINUTES` without a message, or when you log out). Log in again.

- Open the app and the API on the same host name: `localhost` and `127.0.0.1` count as different sites, so the cookie will not be sent if you mix them.

**Chat shows "offline" and never connects (close code 4403)**

- The website you opened is not in `CORS_ORIGINS`. Add its address (for example `http://localhost:5173`) to `backend/orchestrator/.env` and restart the orchestrator.

**"You are sending messages too quickly"**

- You hit the rate limit. Wait for the countdown, or raise `CHAT_RATE_LIMIT_MESSAGES` in `.env` for local testing and restart the orchestrator.

**"Something went wrong" on every message, or the chat list says the service is unavailable**

- The retriever is probably not running or not reachable at `CHAT_SERVICE_ADDRESS`. Start it and try again. The orchestrator reconnects by itself on the next message.

- If the retriever is running but the answer fails, check its window for the error. A missing `GROQ_API_KEY` in `backend/retriever/.env` is a common cause now that the key lives there.

- If you just turned on the token or TLS, check that `CHAT_SERVICE_TOKEN` is identical in both `.env` files and that `CHAT_TLS_CA` points to the certificate that signed the retriever's one. A mismatch looks the same as "service unavailable" or "unauthenticated" in the logs.

**The retriever or the ingestion service refuses to start and talks about a service token**

- `CHAT_SERVICE_TOKEN` (retriever) and `INGEST_SERVICE_TOKEN` (ingestion) are required, and an example value like `change_me` is refused. Make one (see [Setup](#2-environment-files)) and put the same value in the orchestrator's `.env`.

**Orchestrator fails with "INGEST_SERVICE_TOKEN is missing from orchestrator environment"**

- Add `INGEST_SERVICE_TOKEN` to `backend/orchestrator/.env` (the same value as in `backend/ingestion/.env`). The check runs when the module is imported, so it also stops the tests and CI if the variable is not set.

**Orchestrator fails with "Set both CHAT_TLS_CLIENT_CERT and CHAT_TLS_CLIENT_KEY, or neither."**

- Mutual TLS needs both files. Set both, or clear both.

**Answers work but never use older messages**

- Qdrant may be down. The chat still answers from the recent history, and the error is logged in the retriever window. Start Qdrant (`docker compose up -d`).

**Retriever fails to start with "CHAT_ENCRYPTION_KEYS is not set"**

- Generate a key (see [Encryption at rest](#encryption-at-rest)) and add it to `backend/retriever/.env`.

**"Could not decrypt a stored value: wrong key, or the data was changed"**

- The key in `CHAT_ENCRYPTION_KEYS` is not the one that encrypted the data. Put the original key back. If you rotated keys, keep the old one after the new one, separated by a comma.

**My old chats are missing after the upgrade**

- Chats used to be stored by the orchestrator. Stop both services, then from `backend/retriever` run `python migrate_from_orchestrator.py ..\orchestrator\chat.db`, followed by `python encrypt_existing.py --qdrant`. Run them in this order, because the copy step writes plain text.

**Orchestrator fails with "COOKIE_SAMESITE=none requires COOKIE_SECURE=true"**

- Set `COOKIE_SECURE=true` (needs https), or use `COOKIE_SAMESITE=lax` for local development.

**Chat list or messages do not load**

- Check that the orchestrator and the retriever are running and the browser is not showing "API unreachable". The chat list and messages come from the server, so they need both (a failed load shows a Retry button).

**A document is stuck on "Queued" or "Processing"**

- The ingestion **worker** is probably not running. The API only accepts the file; the worker reads and stores it. Start it with `python worker.py` in `backend\ingestion` (or use `run-all.ps1`). A job that was cut off in the middle starts again by itself.

**Uploading says "The document service is unavailable" (or "could not handle the request")**

- The ingestion API is not running at `INGEST_SERVICE_URL` (start it and check http://localhost:8100/health), or `INGEST_SERVICE_TOKEN` is not the same in `backend/orchestrator/.env` and `backend/ingestion/.env`.

**A document ends as "Failed"**

- The message under it says why: the file is damaged or not the type its name says, it has no readable text (scanned pages are not supported, there is no OCR), it is too large or too long, or reading it took too long. Fix or replace the file and upload it again.

**A document keeps showing "Deleting"**

- The search index (Qdrant) could not be reached when you deleted it. Start Qdrant and click the × again.

**I asked about a document and the answer says it is not there**

- If you ticked the document, the AI gets its best parts (or all of it, when it is short), so check that the file really contains the answer and has text (no OCR). If you ticked **nothing**, only parts that clearly match the question are used, so a vaguely worded question can find nothing. Tick the document, or name it in your question.

**The ingestion service fails to start**

- `INGEST_SERVICE_TOKEN` and `CHAT_ENCRYPTION_KEYS` are both required. Use the same `CHAT_ENCRYPTION_KEYS` as the retriever, or document text cannot be read when you ask about it.

## Known limitations

- **Deleting a chat only hides it in this browser.** There is no delete endpoint yet, so the chat (and its memory) stays on the server and shows up again in other browsers.

- **Long documents are searched, not read whole.** A document with more than 4 parts only gives the AI its best matching parts, so a request like "summarize the whole document" only sees those. Shorter documents are given in full.

- **The job queue is a database table** and the worker is a single process. For several workers, move claiming to Redis or a proper queue.

- **Uploaded files sit on disk unencrypted** for the seconds a job runs, then they are deleted. The original files are not kept.

- **Ingestion shares the encryption key with the retriever on purpose** (one trust zone). Keep both in the same secrets manager.

- **The ingestion port must stay on an internal network** (or sit behind something that serves TLS), because the ingestion service trusts the `X-User-Id` it receives, like the gRPC port. The ingestion API has no TLS of its own.

- **No OCR, no web pages, no sharing:** scanned PDFs fail with "no readable text", URLs cannot be ingested, and documents cannot be shared between users.

- **The rate limiter is in memory.** It resets when the server restarts and is not shared between workers (use Redis for that).

- **Encryption covers text at rest only.** Ids, roles, timestamps and the search vectors are not encrypted, and neither are user names and emails in the orchestrator's database. Use a locked-down Qdrant (API key, internal network) and disk encryption for the rest. The encryption key sits in a `.env` file; in production use a secrets manager.

- **TLS between the services is optional and off by default.** Without it the orchestrator and the retriever talk over plain gRPC (still with the service token) and the retriever trusts the user id it receives, so keep that port on an internal network or turn TLS on before deploying. The Qdrant API key is only encrypted in transit if Qdrant is reached over `https`.

- **The ingestion service logs at `INFO` only** and does not carry the request id yet (see [Logging](#logging)).

- **CI does not run the ingestion and shared-package tests.**

## Changing the gRPC contract

`backend/proto/chat.proto` is the only copy of the contract. If you edit it, regenerate the Python files into **both** services from the project root:

```powershell
python -m grpc_tools.protoc -I backend/proto --python_out=backend/retriever --grpc_python_out=backend/retriever backend/proto/chat.proto
python -m grpc_tools.protoc -I backend/proto --python_out=backend/orchestrator --grpc_python_out=backend/orchestrator backend/proto/chat.proto
```

Then restart both services. The generated `*_pb2.py` and `*_pb2_grpc.py` files are not stored in git (`.gitignore` skips them), so a fresh checkout has to generate them with these commands, and CI does the same. A question is one server-streaming `Chat` call (events: `Context`, `Token`, `Done`); history is two simple calls, `ListConversations` and `ListMessages`.

## Screenshots

login:

<img width="842" height="800" alt="image" src="https://github.com/user-attachments/assets/ae838513-470a-4c49-a964-f03f588b5a5e" />

signup:

<img width="1432" height="952" alt="image" src="https://github.com/user-attachments/assets/8d7f11bf-74e6-4f61-b5bb-fde868609ec8" />

chat app:

<img width="1887" height="953" alt="image" src="https://github.com/user-attachments/assets/32d0b6b5-d949-4494-bea3-a4cdd948ab51" />

doc ingestion feature:

<img width="986" height="130" alt="image" src="https://github.com/user-attachments/assets/9607bdee-c3ce-4dbf-be2c-d3556f5505ed" />

doc attachment:

<img width="1783" height="849" alt="image" src="https://github.com/user-attachments/assets/25672914-1c16-4fbc-af99-485d45ddfa52" />

