# Day2 BP Chat

A chat app with a memory. You talk to an AI assistant, and it can remember what you said in your **earlier conversations**. When you ask a question, the app searches your past messages for anything relevant and gives it to the AI along with your question.

> **Status: work in progress.** The chat pipeline, sign up / login and saved chat history are built.

## Features

- **Sign up and login:** create an account with a name, email and password. Passwords are stored hashed, never as plain text.
- **Secure chat connection:** the chat WebSocket only works with a valid login token. Expired or wrong tokens log you out automatically.
- **Chat with an AI:** your messages are answered by an AI model (Groq, `openai/gpt-oss-120b`).
- **Memory across conversations:** the app searches your past messages by meaning and keywords and gives the relevant ones to the AI.
- **Private memory:** search is filtered by your user id, so you only ever see your own messages.
- **Saved chat history:** users, conversations and messages are stored in SQLite (`chat.db`).
- **Live chat screen:** typing indicator, connection status, automatic reconnect, retry for failed messages, and the related past messages shown as context under each answer.
- **One command start (Windows):** `run-all.ps1` starts Qdrant, the retriever, the orchestrator and the frontend.

## How it works (in plain words)

1. You sign up or log in. The backend gives you a **token** that proves who you are.
2. You type a message in the chat window.
3. The app searches your **past messages** for ones related to what you just asked.
4. Your question plus those past messages are sent to an AI model.
5. The AI replies, and the reply appears in the chat.
6. Both your message and the AI's reply are saved, so they can be found next time.

"Related" means related by **meaning**, not just matching words. A question about your "temperature" can find an old message that said "I had a fever".

## Architecture

```text
   ┌──────────────┐
   │   Browser    │   React + TypeScript (Vite)
   │  (Frontend)  │
   └──────┬───────┘
          │  REST + WebSocket (with login token)
          ▼
   ┌──────────────┐        HTTPS         ┌──────────────┐
   │ Orchestrator │ ───────────────────► │   Groq LLM   │
   │   FastAPI    │ ◄─────────────────── │  (AI model)  │
   │  + SQLite    │                      └──────────────┘
   └──────┬───────┘
          │  gRPC
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
| **Frontend** | The login and chat screens. Saves your token in the browser, sends your message and shows the reply. |
| **Orchestrator** | The coordinator. Handles sign up and login, checks your token, asks the retriever for related past messages, asks the AI for an answer, saves both messages in SQLite, and sends the reply back. |
| **SQLite** (`chat.db`) | Stores users, conversations and messages. Lives inside `backend/orchestrator`. |
| **Retriever** | The memory keeper. Converts messages into searchable vectors, saves them, and finds related ones. It is the **only** service that talks to Qdrant. |
| **Qdrant** | The database that stores the message vectors and supports similarity search. |
| **Groq LLM** | The AI model (`openai/gpt-oss-120b`) that writes the reply. It is called only by the orchestrator. |

### Rules the design follows

- Frontend ↔ Orchestrator: REST and WebSocket. The WebSocket needs a valid login token.
- Orchestrator ↔ Retriever: gRPC (the contract is in `backend/proto/retriever.proto`).
- Orchestrator ↔ Groq: HTTPS.
- Retriever ↔ Qdrant: database connection.
- The orchestrator never talks to Qdrant directly, and the AI is not part of the retriever.
- The user's id comes from the token, never from what the browser sends.

### How login works

- `POST /api/signup` and `POST /api/login` return an `access_token`. Passwords are stored hashed (argon2), never as plain text.
- The token is a JWT signed with `SECRET_KEY`. It lasts **1 day**.
- The frontend connects to `/ws/chat?token=...`.
- If the token is missing, wrong or expired, the backend closes the chat connection with code `4401`. The frontend then logs you out automatically, and you just log in again.
- If you change `SECRET_KEY`, all old tokens stop working and everyone must log in again.

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
| Login | JWT (PyJWT), password hashing (pwdlib + argon2) |
| Chat history | SQLite, SQLAlchemy |
| Service-to-service | gRPC, Protocol Buffers |
| Embeddings | Sentence Transformers (dense), FastEmbed (sparse) |
| Vector database | Qdrant |
| AI model | Groq API, `openai/gpt-oss-120b` |
| Containers | Docker (currently only for Qdrant) |

## Folder structure

```text
day2-bp_app/
├── frontend/                 # React app
├── backend/
│   ├── orchestrator/         # FastAPI: REST, WebSocket, calls retriever + Groq
│   │   ├── .env              # your secrets (not in git)
│   │   ├── .env.example
│   │   ├── main.py
│   │   ├── routes.py         # /api/health, /api/signup, /api/login
│   │   ├── websocket.py      # /ws/chat (needs token)
│   │   ├── auth.py           # password hashing + JWT
│   │   ├── database.py       # SQLite connection
│   │   ├── models.py         # users, conversations, messages
│   │   ├── chat.db           # SQLite database (created automatically)
│   │   ├── retriever_client.py
│   │   ├── retriever_pb2.py        # generated from retriever.proto
│   │   ├── retriever_pb2_grpc.py   # generated from retriever.proto
│   │   ├── llm.py
│   │   └── requirements.txt
│   ├── retriever/            # gRPC service: embeddings + Qdrant
│   │   ├── .env
│   │   ├── main.py
│   │   ├── retrieval.py
│   │   ├── embeddings.py
│   │   ├── qdrant_store.py
│   │   ├── retriever_pb2.py        # generated from retriever.proto
│   │   ├── retriever_pb2_grpc.py   # generated from retriever.proto
│   │   └── requirements.txt
│   ├── proto/
│   │   └── retriever.proto   # the gRPC contract
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
SECRET_KEY=your_secret_key_here
```

`SECRET_KEY` is required. The orchestrator will not start without it. Use a long random value and keep it the same between restarts. To make one:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

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

This starts Qdrant (Docker) and opens separate windows for the retriever, orchestrator and frontend. The first retriever start is slow because it downloads the embedding models. Wait for `Retriever gRPC server running on port 50051` before chatting.

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

Then open **http://localhost:5173** and create an account. The frontend must run on port 5173 because the backend's CORS setting only allows that address.

### Quick checks
- API health: http://localhost:8000/api/health should return `{"status": "healthy"}`.
- Qdrant dashboard: http://localhost:6333/dashboard.

## Troubleshooting

**Chat says "forbidden" or sends me back to the login page**
- Your token is missing, wrong or expired (tokens last 1 day, and change if `SECRET_KEY` changes). Log in again.
- Check that `SECRET_KEY` is set in `backend/orchestrator/.env` and was not changed.

**Orchestrator fails with "SECRET_KEY is not configured"**
- Add `SECRET_KEY` to `backend/orchestrator/.env`.

## Changing the gRPC contract

If you edit `backend/proto/retriever.proto`, regenerate the Python files into **both** services from the project root:

powershell
python -m grpc_tools.protoc -I backend/proto --python_out=backend/retriever --grpc_python_out=backend/retriever backend/proto/retriever.proto
python -m grpc_tools.protoc -I backend/proto --python_out=backend/orchestrator --grpc_python_out=backend/orchestrator backend/proto/retriever.proto


Then restart both services.

##screenshot
login:
<img width="842" height="800" alt="image" src="https://github.com/user-attachments/assets/ae838513-470a-4c49-a964-f03f588b5a5e" />
signup:
<img width="1432" height="952" alt="image" src="https://github.com/user-attachments/assets/8d7f11bf-74e6-4f61-b5bb-fde868609ec8" />
chat app:
<img width="1887" height="953" alt="image" src="https://github.com/user-attachments/assets/32d0b6b5-d949-4494-bea3-a4cdd948ab51" />


