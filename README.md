# INFO MATE

A chat app with a memory. You talk to an AI assistant, and it can remember what you said in your **earlier conversations**. When you ask a question, the app searches your past messages for anything relevant and gives it to the AI along with your question.

> **Status: work in progress.** The chat pipeline, sign up / login, saved chat history, rate limiting and automated tests are built.

## Features

- **Sign up and login:** create an account with a name, email and password. Passwords are stored hashed (argon2), never as plain text.
- **Cookie-based sessions:** login uses an HttpOnly session cookie. A session lasts 12 minutes and restarts every time you send a message, so only idle users are logged out.
- **Secure chat connection:** the chat WebSocket checks both the website that opened it (Origin) and your session cookie. A bad or expired session closes the connection and logs you out automatically.
- **Chat with an AI:** your messages are answered by an AI model (Groq, `openai/gpt-oss-120b`).
- **Live answers:** the answer is streamed token by token, and you can stop it halfway (the part already written is kept). Up to 3 different chats can answer at the same time.
- **Memory across conversations:** the app searches your past messages by meaning and keywords and gives the relevant ones to the AI.
- **Private memory:** search is filtered by your user id, so you only ever see your own messages.
- **Saved chat history:** users, sessions, conversations and messages are stored in SQLite (`chat.db`) and loaded in pages, so long histories stay fast.
- **Per-user rate limit:** each user may send 10 messages per 60 seconds (across all their tabs). Over the limit, the app shows a countdown and pauses sending.
- **Connection heartbeat:** the browser pings the server every 25 seconds. A dead connection is detected within about 10 seconds and the app reconnects automatically.
- **Live chat screen:** typing indicator, connection status, automatic reconnect, retry for failed messages, and the related past messages shown as context under each answer.
- **One command start (Windows):** `run-all.ps1` starts Qdrant, the retriever, the orchestrator and the frontend.
- **Automated tests:** 54 backend and 77 frontend tests (see [Tests](#tests)).

## How it works (in plain words)

1. You sign up or log in. The backend gives your browser a **session cookie** that proves who you are.
2. Your chats and their latest messages are loaded from the server (older ones load when you ask for them).
3. You type a message. It travels over a WebSocket.
4. The app searches your **past messages** for ones related to what you just asked.
5. Your question plus those past messages are sent to an AI model.
6. The AI's reply is streamed back as it is written.
7. Both your message and the AI's reply are saved, so they can be found next time.

"Related" means related by **meaning**, not just matching words. A question about your "temperature" can find an old message that said "I had a fever".

## Architecture

```text
   ┌──────────────┐
   │   Browser    │   React + TypeScript (Vite)
   │  (Frontend)  │
   └──────┬───────┘
          │  REST + WebSocket (session cookie)
          ▼
   ┌──────────────┐        HTTPS         ┌──────────────┐
   │ Orchestrator │ ───────────────────► │   Groq LLM   │
   │   FastAPI    │ ◄─────────────────── │  (AI model)  │
   │  + SQLite    │    streamed tokens   └──────────────┘
   └──────┬───────┘
          │  gRPC, bidirectional streaming
          ▼
   ┌──────────────┐
   │  Retriever   │   turns text into vectors, searches and saves
   │ gRPC service │
   └──────┬───────┘
          │  database client
          ▼
   ┌──────────────┐
   │    Qdrant    │   vector database (runs in Docker)
   └──────────────┘
```

### What each part does

| Part | Job |
|---|---|
| **Frontend** | The login and chat screens. Loads chats and messages from the REST API, sends your message, shows the streamed reply, and keeps the connection alive with a heartbeat. The server is the source of truth: the browser only remembers which chat was open and which chats you removed from your list. The login itself is an HttpOnly cookie that JavaScript cannot read. |
| **Orchestrator** | The coordinator. Handles sign up, login and sessions, serves paginated history, checks the WebSocket's origin and cookie, applies the rate limit, asks the retriever for related past messages, streams the AI's answer, saves both messages in SQLite, and sends the reply back. |
| **SQLite** (`chat.db`) | Stores users, sessions, conversations and messages. Lives inside `backend/orchestrator`. |
| **Retriever** | The memory keeper. Converts messages into searchable vectors, saves them, and finds related ones. It is the **only** service that talks to Qdrant. |
| **Qdrant** | The database that stores the message vectors and supports similarity search. |
| **Groq LLM** | The AI model (`openai/gpt-oss-120b`) that writes the reply. It is called only by the orchestrator. |

### Rules the design follows

- Frontend ↔ Orchestrator: REST and WebSocket. Both need a valid session cookie, and the WebSocket also checks the Origin header against `CORS_ORIGINS`.
- Orchestrator ↔ Retriever: gRPC with **bidirectional streaming** (the contract is in `backend/proto/retriever.proto`).
- Orchestrator ↔ Groq: HTTPS, with the answer streamed back token by token.
- Retriever ↔ Qdrant: database connection.
- The orchestrator never talks to Qdrant directly, and the AI is not part of the retriever.
- The user's id comes from the session, never from what the browser sends.

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

**Pagination.** Both history routes use cursors, so new messages never shift the pages:

- Reply shape: `{"items": [...], "next_cursor": "..."}`. `next_cursor` is `null` on the last page.
- Pass `next_cursor` back as `cursor` to get the next page. For messages it leads to **older** messages; items inside a page are always oldest first.
- Defaults: 20 chats and 30 messages per page. `limit` must be 1–100 (otherwise `422`). A bad cursor returns `400`.
- A chat that belongs to someone else returns `404`, the same as a chat that does not exist.

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

- One answer at a time **per chat**, and at most 3 chats answering at once. Extra messages are refused with an error. The server keeps reading while it answers, which is how `cancel` gets through.
- **Heartbeat:** a ping checks that the session is still valid (an expired login is closed with `4401`), but it does **not** extend the session and does **not** count toward the rate limit. Only real chat messages count as activity.
- **Rate limit:** a sliding window per user (default 10 messages per 60 seconds), checked before any database or AI work. Blocked attempts are not counted. The counters live in the server's memory, so they reset on restart and each worker counts separately. For several workers or servers, move the counters to Redis.

### How the retriever link works (gRPC)

`RetrieveData` and `StoreMessage` are both bidirectional streams. The orchestrator keeps **one long-lived stream per call** open and shares it between all users:

- Every request carries a `request_id`, and every reply carries the id of its request, so replies can arrive in any order.
- A search is answered with chunks (best match first), then a final message with `last = true`. A failed request returns an `error` on its final message and does not close the stream.
- The retriever handles each request in its own task, so a slow request does not hold up the others. Embedding work is capped at 4 at a time.
- If the stream breaks (for example the retriever restarts), waiting requests fail with an error and the orchestrator opens a new stream on the next request.
- The user's message is saved to Qdrant in the background while the AI is answering. It is saved **after** the search, so a question cannot find itself.

### How search works

Each saved message is turned into two kinds of vectors:

- **Dense** (`BAAI/bge-base-en-v1.5`): captures *meaning*.
- **Sparse** (`Qdrant/bm25`): captures *keywords*.

A search runs both and merges the two ranked lists with **Reciprocal Rank Fusion (RRF)**. This is called hybrid search. Results are filtered by `user_id` so a user only searches their own messages.

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | React, TypeScript, Vite |
| API | Python, FastAPI (REST + WebSocket) |
| Login | Server-side sessions with an HttpOnly cookie, password hashing (pwdlib + argon2) |
| Chat history | SQLite, SQLAlchemy, cursor pagination |
| Service-to-service | gRPC (`grpc.aio`, bidirectional streaming), Protocol Buffers |
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
│   ├── orchestrator/         # FastAPI: REST, WebSocket, calls retriever + Groq
│   │   ├── .env              # your secrets (not in git)
│   │   ├── .env.example
│   │   ├── main.py           # FastAPI app: CORS, /api routes, /ws/chat
│   │   ├── routes.py         # /api/health, /signup, /login, /logout, /me
│   │   ├── history.py        # /api/conversations and messages (cursor pagination)
│   │   ├── websocket.py      # /ws/chat (cookie + origin check, heartbeat, rate limit, streaming, cancel)
│   │   ├── rate_limit.py     # per-user sliding-window rate limiter
│   │   ├── auth.py           # password hashing, sessions, cookies
│   │   ├── database.py       # SQLite connection
│   │   ├── models.py         # users, sessions, conversations, messages
│   │   ├── chat.db           # SQLite database (created automatically)
│   │   ├── retriever_client.py     # long-lived gRPC streams to the retriever
│   │   ├── retriever_pb2.py        # generated from retriever.proto
│   │   ├── retriever_pb2_grpc.py   # generated from retriever.proto
│   │   ├── llm.py
│   │   ├── pytest.ini
│   │   ├── requirements.txt
│   │   ├── requirements-dev.txt    # adds pytest and httpx
│   │   └── tests/                  # conftest.py, test_rate_limit.py, test_history.py, test_websocket.py
│   ├── retriever/            # gRPC service: embeddings + Qdrant
│   │   ├── .env
│   │   ├── main.py           # async bidirectional-streaming gRPC server
│   │   ├── retrieval.py
│   │   ├── embeddings.py
│   │   ├── qdrant_store.py
│   │   ├── retriever_pb2.py        # generated from retriever.proto
│   │   ├── retriever_pb2_grpc.py   # generated from retriever.proto
│   │   └── requirements.txt
│   ├── proto/
│   │   └── retriever.proto   # the gRPC contract (the only copy)
│   └── requirements.txt
├── docker-compose.yml        # Qdrant
├── run-all.ps1               # starts everything on Windows
└── README.md
```

`chat.db` is created automatically in `backend/orchestrator` the first time the orchestrator starts. It holds user data, so keep it out of git (if it is already tracked, run `git rm --cached backend/orchestrator/chat.db`).

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
pip install -r retriever\requirements.txt
```

### 2. Environment files

`backend/orchestrator/.env`
```env
GROQ_API_KEY=your_key_here
GROQ_MODEL=openai/gpt-oss-120b
RETRIEVER_ADDRESS=localhost:50051
SESSION_MINUTES=12
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
COOKIE_SECURE=false
COOKIE_SAMESITE=lax
CHAT_RATE_LIMIT_MESSAGES=10
CHAT_RATE_LIMIT_WINDOW_SECONDS=60
```

Only `GROQ_API_KEY` is required. The rest have the defaults shown above.

| Setting | What it does |
|---|---|
| `SESSION_MINUTES` | How long an idle login lasts. It restarts on every chat message. |
| `CORS_ORIGINS` | Website addresses allowed to call the API and open the chat WebSocket. |
| `COOKIE_SECURE`, `COOKIE_SAMESITE` | Cookie settings. Local (http): `false` and `lax`. Deployed with the frontend and API on different sites: `true` and `none`. `none` requires `true`, or the orchestrator will not start. |
| `CHAT_RATE_LIMIT_MESSAGES`, `CHAT_RATE_LIMIT_WINDOW_SECONDS` | Each user may send this many messages in any window of this many seconds. |

> `SECRET_KEY` is no longer used. Login used to be a signed JWT, but it is now a server-side session, so you can remove `SECRET_KEY` from your `.env` and `.env.example` (and `PyJWT` from `requirements.txt`).

`backend/retriever/.env`
```env
QDRANT_URL=http://localhost:6333
```

`frontend/.env`
```env
VITE_API_BASE_URL=http://localhost:8000
VITE_WS_URL=ws://localhost:8000/ws/chat
```

**Never commit `.env` files or `chat.db`.** They are listed in `.gitignore`. Only `.env.example` files belong in git.

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

This starts Qdrant (Docker) and opens separate windows for the retriever, orchestrator and frontend. The first retriever start is slow because it downloads the embedding models. Wait for `Retriever gRPC server (bidirectional streaming) running on port 50051` before chatting.

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

The tests need no Qdrant, retriever, Groq key or Docker. The backend tests replace the retriever and the AI with fakes and use a temporary SQLite file per test, so `chat.db` is never touched.

```powershell
# Backend: 54 tests (rate limiter, pagination, WebSocket, heartbeat, auth checks)
cd backend\orchestrator
pytest

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

**"Something went wrong" on every message**
- The retriever is probably not running or not reachable at `RETRIEVER_ADDRESS`. Start it and try again. The orchestrator reconnects to it by itself on the next message.

**Orchestrator fails with "COOKIE_SAMESITE=none requires COOKIE_SECURE=true"**
- Set `COOKIE_SECURE=true` (needs https), or use `COOKIE_SAMESITE=lax` for local development.

**Chat list or messages do not load**
- Check that the orchestrator is running and the browser is not showing "API unreachable". The chat list and messages now come from the server, so they need the API (a failed load shows a Retry button).

## Known limitations

- **Deleting a chat only hides it in this browser.** There is no delete endpoint yet, so the chat (and its memory) stays on the server and shows up again in other browsers.
- **Retrieved context is not stored.** The "related past messages" under an answer only appear for replies received in the current session, not for messages loaded from history.
- **The rate limiter is in memory.** It resets when the server restarts and is not shared between workers (use Redis for that).

## Changing the gRPC contract

`backend/proto/retriever.proto` is the only copy of the contract. If you edit it, regenerate the Python files into **both** services from the project root:

```powershell
python -m grpc_tools.protoc -I backend/proto --python_out=backend/retriever --grpc_python_out=backend/retriever backend/proto/retriever.proto
python -m grpc_tools.protoc -I backend/proto --python_out=backend/orchestrator --grpc_python_out=backend/orchestrator backend/proto/retriever.proto
```

Then restart both services. Both calls are bidirectional streams: every request has a `request_id`, and the matching replies carry the same id.

## Screenshots

login:
<img width="842" height="800" alt="image" src="https://github.com/user-attachments/assets/ae838513-470a-4c49-a964-f03f588b5a5e" />

signup:
<img width="1432" height="952" alt="image" src="https://github.com/user-attachments/assets/8d7f11bf-74e6-4f61-b5bb-fde868609ec8" />

chat app:
<img width="1887" height="953" alt="image" src="https://github.com/user-attachments/assets/32d0b6b5-d949-4494-bea3-a4cdd948ab51" />
