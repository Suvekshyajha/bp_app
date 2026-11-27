#!/usr/bin/env bash
# Starts the backend in GitHub Codespaces. Run it again after every restart
# of the codespace:   bash deploy/codespaces/start.sh
#
# The first run creates deploy/codespaces/.env (secrets, never committed) and
# builds the images, which takes a while.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${HOST_PORT:-8000}"
FRONTEND_DEFAULT="https://info-mate-zeta.vercel.app"

random_token() { openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n'; }
# A Fernet key is 32 random bytes in URL-safe base64, padding included
fernet_key() { openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'; }

if [ ! -f .env ]; then
    key="${GROQ_API_KEY:-}"
    if [ -z "$key" ]; then
        read -r -s -p "Paste your Groq API key (it stays hidden): " key
        echo
    fi
    if [ -z "$key" ]; then
        echo "A Groq API key is needed." >&2
        exit 1
    fi

    (
        umask 077
        cat > .env <<EOF
GROQ_API_KEY=$key
FRONTEND_URL=${FRONTEND_URL:-$FRONTEND_DEFAULT}
QDRANT_API_KEY=$(random_token)
CHAT_SERVICE_TOKEN=$(random_token)
INGEST_SERVICE_TOKEN=$(random_token)
# Losing this key makes the stored chats and documents unreadable
CHAT_ENCRYPTION_KEYS=$(fernet_key)
EOF
    )
    echo "Created deploy/codespaces/.env"
fi

# The embeddings need a Gemini API key. Asked for once, also when .env is older
if ! grep -q '^GEMINI_API_KEY=.' .env; then
    gemini_key="${GEMINI_API_KEY:-}"
    if [ -z "$gemini_key" ]; then
        read -r -s -p "Paste your Gemini API key (it stays hidden): " gemini_key
        echo
    fi
    if [ -z "$gemini_key" ]; then
        echo "A Gemini API key is needed." >&2
        exit 1
    fi
    printf '\nGEMINI_API_KEY=%s\n' "$gemini_key" >> .env
fi

# Codespaces drops large packets, so downloads time out at the default size
# (see compose.yml). Reset at every codespace start, so it is set every time.
if [ -n "${CODESPACE_NAME:-}" ]; then
    if ! sudo ip link set dev eth0 mtu 1400; then
        echo "Warning: could not lower the packet size; downloads may time out." >&2
    fi
fi

docker compose up -d --build

echo "Waiting for the backend (the services need a moment to start)..."

# Asked from inside the network: the retriever opens its port only after the
# models are loaded, and old log lines cannot answer for a new start
services_answer() {
    docker compose exec -T orchestrator python -c "
import socket, urllib.request
socket.create_connection(('retriever', 50051), 2).close()
urllib.request.urlopen('http://ingestion:8100/health', timeout=2)
" >/dev/null 2>&1
}

ready=""
for _ in $(seq 1 90); do
    if curl -fsS "http://localhost:$PORT/api/health" >/dev/null 2>&1 && services_answer; then
        ready=1
        break
    fi
    sleep 2
done

if [ -z "$ready" ]; then
    echo "The backend did not become ready. Look at:  cd deploy/codespaces && docker compose logs" >&2
    exit 1
fi

echo
echo "Backend is running."

# The answers come from Groq: make sure the retriever can reach it
if ! docker compose exec -T retriever python -c "
import socket, ssl
raw = socket.create_connection(('api.groq.com', 443), 15)
ssl.create_default_context().wrap_socket(raw, server_hostname='api.groq.com').close()
" >/dev/null 2>&1; then
    echo "Warning: the retriever cannot reach api.groq.com, so chat answers will fail." >&2
fi

if [ -z "${CODESPACE_NAME:-}" ]; then
    echo "Not inside a codespace. Local address: http://localhost:$PORT"
    exit 0
fi

url="https://${CODESPACE_NAME}-${PORT}.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"

if gh codespace ports visibility "$PORT:public" -c "$CODESPACE_NAME" >/dev/null 2>&1; then
    echo "Port $PORT is now Public."
else
    echo "Do this by hand: open the PORTS tab, right-click port $PORT,"
    echo "Port Visibility, Public."
fi

echo
echo "VITE_API_BASE_URL = $url"
echo "VITE_WS_URL       = wss://${url#https://}/ws/chat"
echo
echo "Check: open $url/api/health in your browser."
