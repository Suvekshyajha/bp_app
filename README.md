# Day2 BP Chat

A chat app with a memory. You talk to an AI assistant, and it can remember what you said in your **earlier conversations**. When you ask a question, the app searches your past messages for anything relevant and gives it to the AI along with your question.

> **Status: work in progress.** The chat pipeline is built. Real login, saved chat history and conversation management are not built yet (see [Roadmap](#roadmap)).

## How it works (in plain words)

1. You type a message in the chat window.
2. The app searches your **past messages** for ones related to what you just asked.
3. Your question plus those past messages are sent to an AI model.
4. The AI replies, and the reply appears in the chat.
5. Both your message and the AI's reply are saved, so they can be found next time.

"Related" means related by **meaning**, not just matching words. A question about your "temperature" can find an old message that said "I had a fever".

## Architecture

```text
   ┌──────────────┐
   │   Browser    │   React + TypeScript (Vite)
   │  (Frontend)  │
   └──────┬───────┘
          │  REST + WebSocket
          ▼
   ┌──────────────┐        HTTPS         ┌──────────────┐
   │ Orchestrator │ ───────────────────► │   Groq LLM   │
   │   FastAPI    │ ◄─────────────────── │  (AI model)  │
   └──────┬───────┘                      └──────────────┘
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
| **Frontend** | The chat screen. Sends your message and shows the reply. |
| **Orchestrator** | The coordinator. Receives your message, asks the retriever for related past messages, asks the AI for an answer, saves both messages, and sends the reply back. |
| **Retriever** | The memory keeper. Converts messages into searchable vectors, saves them, and finds related ones. It is the **only** service that talks to Qdrant. |
| **Qdrant** | The database that stores the message vectors and supports similarity search. |
| **Groq LLM** | The AI model (`openai/gpt-oss-120b`) that writes the reply. It is called only by the orchestrator. |

### Rules the design follows

- Frontend ↔ Orchestrator: REST and WebSocket.
- Orchestrator ↔ Retriever: gRPC (the contract is in `backend/proto/retriever.proto`).
- Orchestrator ↔ Groq: HTTPS.
- Retriever ↔ Qdrant: database connection.
- The orchestrator never talks to Qdrant directly, and the AI is not part of the retriever.

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
│   │   ├── main.py
│   │   ├── routes.py         # /api/health
│   │   ├── websocket.py      # /ws/chat
│   │   ├── retriever_client.py
│   │   └── llm.py
│   ├── retriever/            # gRPC service: embeddings + Qdrant
│   │   ├── main.py
│   │   ├── retrieval.py
│   │   ├── embeddings.py
│   │   └── qdrant_store.py
│   └── proto/
│       └── retriever.proto   # the gRPC contract
├── docker-compose.yml        # Qdrant
├── run-all.ps1               # starts everything on Windows
└── README.md
```

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

**Never commit `.env` files.** They are listed in `.gitignore`. Only `.env.example` files belong in git.

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

Then open **http://localhost:5173**. The frontend must run on port 5173 because the backend's CORS setting only allows that address.

### Quick checks
- API health: http://localhost:8000/api/health should return `{"status": "healthy"}`.
- Qdrant dashboard: http://localhost:6333/dashboard.

## Changing the gRPC contract

If you edit `backend/proto/retriever.proto`, regenerate the Python files into **both** services from the project root:

```powershell
python -m grpc_tools.protoc -I backend/proto --python_out=backend/retriever --grpc_python_out=backend/retriever backend/proto/retriever.proto
python -m grpc_tools.protoc -I backend/proto --python_out=backend/orchestrator --grpc_python_out=backend/orchestrator backend/proto/retriever.proto
```

Then restart both services.

## Roadmap

Not built yet:

- [ ] Real login and authentication (right now the frontend uses a temporary demo identity, and the backend trusts the `user_id` it is sent)
- [ ] SQL database (SQLite, later Postgres) for users, conversations and ordered chat history
- [ ] Conversation list and history endpoints
- [ ] Recent messages of the current chat sent to the AI (it currently only sees searched past snippets)
- [ ] Filtering search by date (for questions like "what was my fever on Tuesday?")
- [ ] Automated tests
- [ ] Docker setup for all services

## Known limitations

- No real security yet: do not use with real personal data until authentication is added.
- The AI sees only searched snippets from the past, not the full recent conversation.
- If the AI call fails after your message was saved, your message stays saved without a reply.
