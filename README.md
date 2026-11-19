#INFO MATE
A chat app with a memory. You talk to an AI assistant, and it can remember what you said in your **earlier conversations**. When you ask a question, the app searches your past messages for anything relevant and gives it to the AI along with your question.

> **Status: work in progress.** The chat pipeline, sign up / login and saved chat history are built.

## Features

- **Sign up and login:** create an account with a name, email and password. Passwords are stored hashed (argon2), never as plain text.
- **Cookie-based sessions:** login uses an HttpOnly session cookie. A session lasts 12 minutes and restarts every time you send a message, so only idle users are logged out.
- **Secure chat connection:** the chat WebSocket checks both the website that opened it (Origin) and your session cookie. A bad or expired session closes the connection and logs you out automatically.
- **Chat with an AI:** your messages are answered by an AI model (Groq, `openai/gpt-oss-120b`).
- **Live answers:** the answer is streamed to the screen token by token, and you can stop it halfway (the part already written is kept).
- **Memory across conversations:** the app searches your past messages by meaning and keywords and gives the relevant ones to the AI.
- **Private memory:** search is filtered by your user id, so you only ever see your own messages.
- **Saved chat history:** users, sessions, conversations and messages are stored in SQLite (`chat.db`).
- **Live chat screen:** typing indicator, connection status, automatic reconnect, retry for failed messages, and the related past messages shown as context under each answer.
- **One command start (Windows):** `run-all.ps1` starts Qdrant, the retriever, the orchestrator and the frontend.

## How it works (in plain words)

1. You sign up or log in. The backend gives your browser a **session cookie** that proves who you are.
2. You type a message in the chat window. It travels over a WebSocket.
3. The app searches your **past messages** for ones related to what you just asked.
4. Your question plus those past messages are sent to an AI model.
5. The AI's reply is streamed back to the chat as it is written.
6. Both your message and the AI's reply are saved, so they can be found next time.

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
| **Frontend** | The login and chat screens. Sends your message, shows the streamed reply, and keeps a copy of the messages in the browser (localStorage). The login itself is an HttpOnly cookie that JavaScript cannot read. |
| **Orchestrator** | The coordinator. Handles sign up, login and sessions, checks the WebSocket's origin and cookie, asks the retriever for related past messages, streams the AI's answer, saves both messages in SQLite, and sends the reply back. |
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

### How login works

- `POST /api/signup` and `POST /api/login` create a session and set it in an **HttpOnly cookie** named `session_id`. No token is returned in the response body. Passwords are stored hashed (argon2).
- The cookie holds a random value. Only its SHA-256 hash is stored in the `sessions` table, so a leaked database cannot be used to log in.
- A session lasts `SESSION_MINUTES` (default **12**) and restarts every time you send a chat message. The cookie itself lives 7 days, but the real limit is the database.
- `GET /api/me` tells the frontend who is logged in. `POST /api/logout` deletes the session on the server, so the cookie stops working everywhere.
- The frontend connects to `/ws/chat` (no token in the URL; the browser sends the cookie by itself).
- The backend closes the chat connection with code `4401` if the session is missing, wrong or expired (the frontend then logs you out), and with code `4403` if the website that opened it is not in `CORS_ORIGINS`.
- Cookies are sent between the page and the API only if both use the same host. Open the app as `localhost` **or** `127.0.0.1`, not a mix of both.

### How a chat message flows over the WebSocket

Messages are JSON.

| Direction | Message | Meaning |
|---|---|---|
| Browser → server | `{"query": "...", "conversation_id": "..."}` | Ask a question |
| Browser → server | `{"type": "cancel"}` | Stop the answer that is being written |
| Server → browser | `{"type": "token", "content": "..."}` | One piece of the answer |
| Server → browser | `{"type": "done", "answer": "...", "results": [...], "cancelled": false}` | The answer is complete (`cancelled: true` if you stopped it; `answer` then holds what was written so far) |
| Server → browser | `{"error": "..."}` | Something went wrong |

The connection answers one question at a time. A second question sent while an answer is being written is refused with an error. The server keeps reading messages while it answers, which is how `cancel` gets through.

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
| Chat history | SQLite, SQLAlchemy |
| Service-to-service | gRPC (`grpc.aio`, bidirectional streaming), Protocol Buffers |
| Embeddings | Sentence Transformers (dense), FastEmbed (sparse) |
| Vector database | Qdrant |
| AI model | Groq API, `openai/gpt-oss-120b` (streamed) |
| Containers | Docker (currently only for Qdrant) |

## Folder structure

```text
day2-bp_app/
├── frontend/                 # React app
├── backend/
│   ├── orchestrator/         # FastAPI: REST, WebSocket, calls retriever + Groq
│   │   ├── .env              # your secrets (not in git)
│   │   ├── .env.example
│   │   ├── main.py           # FastAPI app: CORS, /api routes, /ws/chat
│   │   ├── routes.py         # /api/health, /signup, /login, /logout, /me
│   │   ├── websocket.py      # /ws/chat (cookie + origin check, streaming, cancel)
│   │   ├── auth.py           # password hashing, sessions, cookies
│   │   ├── database.py       # SQLite connection
│   │   ├── models.py         # users, sessions, conversations, messages
│   │   ├── chat.db           # SQLite database (created automatically)
│   │   ├── retriever_client.py     # long-lived gRPC streams to the retriever
│   │   ├── retriever_pb2.py        # generated from retriever.proto
│   │   ├── retriever_pb2_grpc.py   # generated from retriever.proto
│   │   ├── llm.py
│   │   └── requirements.txt
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
- Node.js 18+
- Docker Desktop
- A free [Groq API key](https://console.groq.com/)

### 1. Python environment (one venv for the backend)

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r orchestrator\requirements.txt
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
```

Only `GROQ_API_KEY` is required. The rest have the defaults shown above.

| Setting | What it does |
|---|---|
| `SESSION_MINUTES` | How long an idle login lasts. It restarts on every chat message. |
| `CORS_ORIGINS` | Website addresses allowed to call the API and open the chat WebSocket. |
| `COOKIE_SECURE`, `COOKIE_SAMESITE` | Cookie settings. Local (http): `false` and `lax`. Deployed with the frontend and API on different sites: `true` and `none`. `none` requires `true`, or the orchestrator will not start. |

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

## Troubleshooting

**Chat sends me back to the login page**
- Your session is missing, wrong or expired (it ends after `SESSION_MINUTES` without a message, or when you log out). Log in again.
- Open the app and the API on the same host name: `localhost` and `127.0.0.1` count as different sites, so the cookie will not be sent if you mix them.

**Chat shows "offline" and never connects (close code 4403)**
- The website you opened is not in `CORS_ORIGINS`. Add its address (for example `http://localhost:5173`) to `backend/orchestrator/.env` and restart the orchestrator.

**"Something went wrong" on every message**
- The retriever is probably not running or not reachable at `RETRIEVER_ADDRESS`. Start it and try again. The orchestrator reconnects to it by itself on the next message.

**Orchestrator fails with "COOKIE_SAMESITE=none requires COOKIE_SECURE=true"**
- Set `COOKIE_SECURE=true` (needs https), or use `COOKIE_SAMESITE=lax` for local development.

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