# INFO MATE

A chat app with a memory. You talk to an AI assistant, and it can remember what you said earlier in the conversation. When you ask a question, the app searches your older messages for anything relevant and gives it to the AI along with your question. Everything that is saved is **encrypted**, so a copy of the database or of Qdrant's storage is unreadable without the server's key.

> **Status: work in progress.** The chat pipeline, sign up / login, saved chat history, rate limiting, encryption at rest and automated tests are built.

## Features

- **Sign up and login:** create an account with a name, email and password. Passwords are stored hashed (argon2), never as plain text.
- **Cookie-based sessions:** login uses an HttpOnly session cookie. A session lasts 12 minutes and restarts every time you send a message, so only idle users are logged out.
- **Secure chat connection:** the chat WebSocket checks both the website that opened it (Origin) and your session cookie. A bad or expired session closes the connection and logs you out automatically.
- **Chat with an AI:** your messages are answered by an AI model (Groq, `openai/gpt-oss-120b`).
- **Live answers:** the answer is streamed token by token, and you can stop it halfway (the part already written is kept). Up to 3 different chats can answer at the same time.
- **Memory inside a chat:** the app searches the older messages of your chat by meaning and keywords and gives the relevant ones to the AI, on top of the recent history.
- **Private memory:** search is filtered by your user id and the chat id, so you only ever get your own messages.
- **Encrypted at rest:** message text and chat titles are encrypted before they are written to SQLite, and the message text stored in Qdrant is encrypted too. Only the retriever holds the key (see [Encryption at rest](#encryption-at-rest)).
- **Saved chat history:** conversations and messages are stored by the retriever in its own SQLite database and loaded in pages, so long histories stay fast. Users and sessions live in the orchestrator's SQLite database.
- **Search failures do not break chat:** if Qdrant is down, the answer is still written from the chat history, just without the extra related messages.
- **Per-user rate limit:** each user may send 10 messages per 60 seconds (across all their tabs). Over the limit, the app shows a countdown and pauses sending.
- **Connection heartbeat:** the browser pings the server every 25 seconds. A dead connection is detected within about 10 seconds and the app reconnects automatically.
- **Live chat screen:** typing indicator, connection status, automatic reconnect, retry for failed messages, and the related past messages shown as context under each answer.
- **One command start (Windows):** `run-all.ps1` starts Qdrant, the retriever, the orchestrator and the frontend.
- **Automated tests:** 46 orchestrator, 52 retriever and 77 frontend tests (see [Tests](#tests)).

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
                                       │  gRPC, internal network only
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

### What each part does

| Part | Job |
|---|---|
| **Frontend** | The login and chat screens. Loads chats and messages from the REST API, sends your message, shows the streamed reply, and keeps the connection alive with a heartbeat. The server is the source of truth: the browser only remembers which chat was open and which chats you removed from your list. The login itself is an HttpOnly cookie that JavaScript cannot read. |
| **Orchestrator** | The front door. Handles sign up, login and sessions, checks the WebSocket's origin and cookie, applies the rate limit, and sends each question to the retriever. It relays what comes back (related messages, tokens, the final answer) to the browser and serves paginated history by asking the retriever. It no longer saves messages or calls the AI itself. |
| **SQLite, orchestrator** (`chat.db`) | Stores users and sessions only. Lives inside `backend/orchestrator`. |
| **Retriever** (chat service, folder `backend/retriever`) | The part that owns the conversation. For each question it checks that the chat belongs to the user, loads the recent history, searches related older messages, calls the AI, streams the answer, and saves both messages. It also converts messages into searchable vectors and is the **only** service that talks to Qdrant, Groq and the message database. It holds the encryption key. |
| **SQLite, retriever** (`chat.db`) | Stores conversations and messages (text and titles encrypted). Lives inside `backend/retriever`. |
| **Qdrant** | The database that stores the message vectors and supports similarity search. The message text kept next to each vector is encrypted. |
| **Groq LLM** | The AI model (`openai/gpt-oss-120b`) that writes the reply. It is called only by the retriever. |

### Rules the design follows

- Frontend ↔ Orchestrator: REST and WebSocket. Both need a valid session cookie, and the WebSocket also checks the Origin header against `CORS_ORIGINS`.
- Orchestrator ↔ Retriever: gRPC (the contract is in `backend/proto/chat.proto`). A question is **one server-streaming call**; history is two plain request/response calls.
- Retriever ↔ Groq: HTTPS, with the answer streamed back token by token.
- Retriever ↔ Qdrant: database connection.
- The orchestrator never talks to Qdrant, Groq or the message database directly.
- The user's id comes from the session, never from what the browser sends. The orchestrator passes it to the retriever, which trusts it, so **keep gRPC port 50051 on an internal network** and never expose it publicly.
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

The two history routes are answered by the retriever; the orchestrator checks the session and forwards the request.

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

- One answer at a time **per chat**, and at most 3 chats answering at once. Extra messages are refused with an error. The server keeps reading while it answers, which is how `cancel` gets through. Cancelling closes the gRPC call, and the retriever saves the part of the answer written so far.
- **Heartbeat:** a ping checks that the session is still valid (an expired login is closed with `4401`), but it does **not** extend the session and does **not** count toward the rate limit. Only real chat messages count as activity.
- **Rate limit:** a sliding window per user (default 10 messages per 60 seconds), checked before any database or AI work. Blocked attempts are not counted. The counters live in the server's memory, so they reset on restart and each worker counts separately. For several workers or servers, move the counters to Redis.

### How the retriever link works (gRPC)

The retriever runs two gRPC services on port 50051 (`backend/proto/chat.proto`):

| Service | Call | Kind |
|---|---|---|
| `ChatService` | `Chat` | One request, a **stream of events** back |
| `HistoryService` | `ListConversations`, `ListMessages` | Plain request / response |

**One question = one call.** The orchestrator sends `user_id`, `conversation_id` and `query`, and receives events in this order:

1. `Context`: once, after the search, before the first token (the related older messages).
2. `Token`: many, one per piece of the answer.
3. `Done`: once, last (ids of the saved messages and the full answer).

There is no shared stream between users any more, so a slow answer never holds up another. The stop button cancels the call.

**Inside the retriever** (`turn.py`), each question runs these steps:

1. Check the chat exists and belongs to this user (it is created on the first message).
2. Load the latest messages of the chat as history, before the new question is saved.
3. Search related older messages. Ones already in the history are dropped so nothing is sent twice.
4. Save the question, then index it in Qdrant in the background. It is saved **after** the search, so a question cannot find itself.
5. Stream the answer from Groq.
6. Save the answer, then index it in the background.

**What this means in practice**

- If the search fails (for example Qdrant is down), the answer is written from the chat history alone and the error is logged.
- `done` is sent as soon as the answer is saved in SQLite. Indexing in Qdrant may still be running, but the next question already sees the answer through the history.
- If the model call fails, nothing half-written is saved. If you press stop (or close the browser), the partial answer is saved.
- Embedding and search work is capped at 4 jobs at a time.

**Errors** become gRPC status codes, which the orchestrator turns into WebSocket errors or HTTP codes: `NOT_FOUND` (chat of someone else, shown as `404`), `INVALID_ARGUMENT` (bad cursor, `400`), `RESOURCE_EXHAUSTED` (model busy), `DEADLINE_EXCEEDED` (model too slow, `504`), `UNAVAILABLE` (model or service unreachable, `503`) and `INTERNAL`.

### How search works

Each saved message is turned into two kinds of vectors:

- **Dense** (`BAAI/bge-base-en-v1.5`): captures *meaning*.
- **Sparse** (`Qdrant/bm25`): captures *keywords*.

A search runs both and merges the two ranked lists with **Reciprocal Rank Fusion (RRF)**. This is called hybrid search. Results are filtered by `user_id` and `conversation_id`, so a user only searches their own messages in that chat. Matches whose dense similarity is below a threshold (default 0.5, set with `RETRIEVAL_MIN_SIMILARITY`) are dropped, so unrelated messages are never handed to the AI.

The vectors are always made from the plain text, **before** the text is encrypted for storage.

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
| **Encrypted** | `messages.content` and `conversations.title` in SQLite, and the `content` field of each Qdrant payload. |
| **Not encrypted** | Ids, roles and timestamps (needed for queries), the search vectors themselves (needed for search), and the user names and emails in the orchestrator's database. |
| **Key** | `CHAT_ENCRYPTION_KEYS` in `backend/retriever/.env`. Only the retriever reads it. |
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

**Key rules**

- **Back the key up** somewhere safe and separate. If it is lost, the stored chats cannot be recovered.
- **Never commit it.** Keep `.env` out of git. In production, load it from a secrets manager.
- **Rotation:** put the new key first and keep the old one after it, comma separated: `CHAT_ENCRYPTION_KEYS=newkey,oldkey`. New data uses the first key; all keys can read.
- A wrong or missing key stops the service with a clear error instead of returning garbage.
- This protects data **at rest**. Someone who controls the running server and its `.env` can still read the chats.

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | React, TypeScript, Vite |
| API | Python, FastAPI (REST + WebSocket) |
| Login | Server-side sessions with an HttpOnly cookie, password hashing (pwdlib + argon2) |
| Chat history | SQLite, SQLAlchemy, cursor pagination (in the retriever) |
| Encryption | `cryptography` (Fernet), application-level, for messages, titles and Qdrant payload text |
| Service-to-service | gRPC (`grpc.aio`, server streaming for chat), Protocol Buffers |
| Embeddings | Sentence Transformers (dense), FastEmbed (sparse) |
| Vector database | Qdrant |
| AI model | Groq API, `openai/gpt-oss-120b` (streamed) |
| Tests | pytest (backend), Vitest + Testing Library (frontend) |
| Containers | Docker (currently only for Qdrant) |

## Folder structure

```text
day2-bp_app/
├── frontend/                 # React app
│   ├── vite.config.ts        # Vite + Vitest config
│   └── src/
│       ├── api.ts            # REST calls (+ their tests: api.test.ts)
│       ├── types.ts
│       ├── services/         # chatSocket.ts (WebSocket + heartbeat), storage, identity
│       ├── hooks/            # useChatSocket, useConversations, useChatMessages
│       ├── components/       # Sidebar, MessageList, ...
│       └── pages/            # LoginPage, ChatPage
│                             # *.test.ts files sit next to the code they test
├── backend/
│   ├── orchestrator/         # FastAPI: login, sessions, rate limit, REST + WebSocket relay
│   │   ├── .env              # your settings (not in git)
│   │   ├── .env.example
│   │   ├── main.py           # FastAPI app: CORS, /api routes, /ws/chat
│   │   ├── routes.py         # /api/health, /signup, /login, /logout, /me
│   │   ├── history.py        # /api/conversations and messages (forwards to the retriever)
│   │   ├── websocket.py      # /ws/chat (cookie + origin check, heartbeat, rate limit, relay, cancel)
│   │   ├── rate_limit.py     # per-user sliding-window rate limiter
│   │   ├── auth.py           # password hashing, sessions, cookies
│   │   ├── database.py       # SQLite connection
│   │   ├── models.py         # users and sessions
│   │   ├── chat.db           # SQLite database: users and sessions (created automatically)
│   │   ├── chat_client.py    # gRPC client for the retriever
│   │   ├── chat_pb2.py       # generated from chat.proto
│   │   ├── chat_pb2_grpc.py  # generated from chat.proto
│   │   ├── pytest.ini
│   │   ├── requirements.txt
│   │   ├── requirements-dev.txt    # adds pytest and httpx
│   │   └── tests/                  # conftest.py, test_rate_limit.py, test_history.py, test_Websocket.py
│   ├── retriever/            # the retriever (also called the chat service): turns, history, search, LLM, encryption
│   │   ├── .env              # QDRANT_URL, GROQ_*, CHAT_ENCRYPTION_KEYS (not in git)
│   │   ├── main.py           # gRPC server: ChatService and HistoryService
│   │   ├── turn.py           # one chat turn: search, save, ask the LLM, save the answer
│   │   ├── repository.py     # all SQLite reads and writes, cursor pagination
│   │   ├── database.py       # SQLite connection
│   │   ├── models.py         # conversations and messages (encrypted columns)
│   │   ├── chat.db           # SQLite database: conversations and messages (created automatically)
│   │   ├── crypto.py         # encryption: key loading and the EncryptedText column type
│   │   ├── encrypt_existing.py          # one-off: encrypt data stored as plain text
│   │   ├── migrate_from_orchestrator.py # one-off: copy old chats from the orchestrator's chat.db
│   │   ├── llm.py            # Groq streaming
│   │   ├── prompt_budget.py  # keeps the history inside the prompt size limit
│   │   ├── retrieval.py      # hybrid search (decrypts the payload text)
│   │   ├── embeddings.py     # dense and sparse vectors
│   │   ├── qdrant_store.py   # Qdrant collection and saving (encrypts the payload text)
│   │   ├── chat_pb2.py       # generated from chat.proto
│   │   ├── chat_pb2_grpc.py  # generated from chat.proto
│   │   ├── test_retrieval.py # tests of the real search code
│   │   ├── pytest.ini
│   │   ├── requirements.txt
│   │   └── tests/            # conftest.py, test_turn.py, test_repository.py, test_grpc.py
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
- Python 3.10+
- Node.js 18+ (a recent version is recommended for Vitest)
- Docker Desktop
- A free [Groq API key](https://console.groq.com/)

### 1. Python environment (one venv for the backend)

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r orchestrator\requirements-dev.txt   # includes requirements.txt + test tools
pip install -r retriever\requirements.txt          # includes groq, SQLAlchemy and cryptography
```

### 2. Environment files

`backend/orchestrator/.env`
```env
RETRIEVER_ADDRESS=localhost:50051
SESSION_MINUTES=12
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
COOKIE_SECURE=false
COOKIE_SAMESITE=lax
CHAT_RATE_LIMIT_MESSAGES=10
CHAT_RATE_LIMIT_WINDOW_SECONDS=60

```

All of these have the defaults shown above. The orchestrator no longer needs any `GROQ_*` setting.

| Setting | What it does |
|---|---|
| `RETRIEVER_ADDRESS` | Where the retriever listens. `CHAT_SERVICE_ADDRESS` can be used instead and takes priority. |
| `SESSION_MINUTES` | How long an idle login lasts. It restarts on every chat message. |
| `CORS_ORIGINS` | Website addresses allowed to call the API and open the chat WebSocket. |
| `COOKIE_SECURE`, `COOKIE_SAMESITE` | Cookie settings. Local (http): `false` and `lax`. Deployed with the frontend and API on different sites: `true` and `none`. `none` requires `true`, or the orchestrator will not start. |
| `CHAT_RATE_LIMIT_MESSAGES`, `CHAT_RATE_LIMIT_WINDOW_SECONDS` | Each user may send this many messages in any window of this many seconds. |

> `SECRET_KEY` is no longer used. Login used to be a signed JWT, but it is now a server-side session, so you can remove `SECRET_KEY` from your `.env` and `.env.example` (and `PyJWT` from `requirements.txt`).

`backend/retriever/.env`
```env
QDRANT_URL=http://localhost:6333
GROQ_API_KEY=your_key_here
GROQ_MODEL=openai/gpt-oss-120b
CHAT_ENCRYPTION_KEYS=your_generated_key_here
```

`QDRANT_URL`, `GROQ_API_KEY` and `CHAT_ENCRYPTION_KEYS` are required. The service will not start without them.

| Setting | What it does |
|---|---|
| `GROQ_API_KEY`, `GROQ_MODEL` | Moved here from the orchestrator. The retriever is the only part that calls the AI. |
| `CHAT_ENCRYPTION_KEYS` | The key that encrypts stored messages and titles. Generate it with the command in [Encryption at rest](#encryption-at-rest). Back it up; never commit it. |
| `CHAT_DB_PATH` | Optional. Keeps the retriever's `chat.db` somewhere other than `backend/retriever`. |
| `RETRIEVAL_MIN_SIMILARITY` | Optional. Minimum similarity (0 to 1) for a related message to be used. Default 0.5. |

`frontend/.env`
```env
VITE_API_BASE_URL=http://localhost:8000
VITE_WS_URL=ws://localhost:8000/ws/chat
```

**Never commit `.env` files, either `chat.db`, or the encryption key.** They are listed in `.gitignore`. Only `.env.example` files belong in git.

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

This starts Qdrant (Docker) and opens separate windows for the retriever, orchestrator and frontend. The first retriever start is slow because it downloads the embedding models. Wait for `Chat service running on port 50051` before chatting.

### Option B: manually

```powershell
# 1. Qdrant
docker compose up -d

# 2. Retriever (in backend\retriever, venv active)
python main.py

# 3. Orchestrator (in backend\orchestrator, venv active)
uvicorn main:app --reload --port 8000

# 4. Frontend (in frontend)
npm run dev
```

Start the retriever before the orchestrator. Then open **http://localhost:5173** and create an account. The frontend must run on an address listed in `CORS_ORIGINS` (by default port 5173) because the backend only allows those addresses.

### Quick checks
- API health: http://localhost:8000/api/health should return `{"status": "healthy"}`.
- Qdrant dashboard: http://localhost:6333/dashboard.

## Tests

The tests need no Qdrant, Groq key or Docker. The backend tests replace the retriever, Qdrant and the AI with fakes and use a temporary SQLite file per test, so the real `chat.db` files are never touched. The retriever tests use a throw-away encryption key.

```powershell
# Orchestrator: 46 tests (rate limiter, pagination, WebSocket relay, heartbeat, auth checks)
cd backend\orchestrator
pytest

# Retriever: 52 tests (turns, repository, gRPC, encrypted columns)
cd backend\retriever
pytest

# Real search code: run on its own, because the tests above replace this module with a fake
pytest test_retrieval.py

# Frontend: 77 tests (API calls, heartbeat, rate-limit handling, history hooks)
cd frontend
npm test
```

Useful variations:

```powershell
pytest tests/test_history.py          # one backend file
pytest -k rate                        # tests whose name contains "rate"
npx vitest                            # frontend watch mode (re-runs on save)
npx vitest run src/api.test.ts        # one frontend file
```

## Troubleshooting

**Chat sends me back to the login page**
- Your session is missing, wrong or expired (it ends after `SESSION_MINUTES` without a message, or when you log out). Log in again.
- Open the app and the API on the same host name: `localhost` and `127.0.0.1` count as different sites, so the cookie will not be sent if you mix them.

**Chat shows "offline" and never connects (close code 4403)**
- The website you opened is not in `CORS_ORIGINS`. Add its address (for example `http://localhost:5173`) to `backend/orchestrator/.env` and restart the orchestrator.

**"You are sending messages too quickly"**
- You hit the rate limit. Wait for the countdown, or raise `CHAT_RATE_LIMIT_MESSAGES` in `.env` for local testing and restart the orchestrator.

**"Something went wrong" on every message, or the chat list says the service is unavailable**
- The retriever is probably not running or not reachable at `RETRIEVER_ADDRESS`. Start it and try again. The orchestrator reconnects by itself on the next message.
- If the retriever is running but the answer fails, check its window for the error. A missing `GROQ_API_KEY` in `backend/retriever/.env` is a common cause now that the key lives there.

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

## Known limitations

- **Deleting a chat only hides it in this browser.** There is no delete endpoint yet, so the chat (and its memory) stays on the server and shows up again in other browsers.
- **Retrieved context is not stored.** The "related past messages" under an answer only appear for replies received in the current session, not for messages loaded from history.
- **The rate limiter is in memory.** It resets when the server restarts and is not shared between workers (use Redis for that).
- **Encryption covers text at rest only.** Ids, roles, timestamps and the search vectors are not encrypted, and neither are user names and emails in the orchestrator's database. Use a locked-down Qdrant (API key, internal network) and disk encryption for the rest. The encryption key sits in a `.env` file; in production use a secrets manager.
- **No encryption between the services.** The orchestrator and retriever talk over plain gRPC and the retriever trusts the user id it receives, so keep that port on an internal network (or add TLS) before deploying.

## Changing the gRPC contract

`backend/proto/chat.proto` is the only copy of the contract. If you edit it, regenerate the Python files into **both** services from the project root:

```powershell
python -m grpc_tools.protoc -I backend/proto --python_out=backend/retriever --grpc_python_out=backend/retriever backend/proto/chat.proto
python -m grpc_tools.protoc -I backend/proto --python_out=backend/orchestrator --grpc_python_out=backend/orchestrator backend/proto/chat.proto
```

Then restart both services. A question is one server-streaming `Chat` call (events: `Context`, `Token`, `Done`); history is two simple calls, `ListConversations` and `ListMessages`.

## Screenshots

login:
<img width="842" height="800" alt="image" src="https://github.com/user-attachments/assets/ae838513-470a-4c49-a964-f03f588b5a5e" />

signup:
<img width="1432" height="952" alt="image" src="https://github.com/user-attachments/assets/8d7f11bf-74e6-4f61-b5bb-fde868609ec8" />

chat app:
<img width="1887" height="953" alt="image" src="https://github.com/user-attachments/assets/32d0b6b5-d949-4494-bea3-a4cdd948ab51" />
