#!/usr/bin/env bash
# Runs the whole backend in one container: the retriever, the ingestion API,
# the ingestion worker and the orchestrator. Only the orchestrator is public.
# If any of them ends, the others are stopped and the container exits, so the
# host starts it again.

# The services share these secrets only with each other, so new ones at every
# start are fine. A value that is already set is kept.
new_token() { python -c "import secrets; print(secrets.token_urlsafe(32))"; }
export CHAT_SERVICE_TOKEN="${CHAT_SERVICE_TOKEN:-$(new_token)}"
export INGEST_SERVICE_TOKEN="${INGEST_SERVICE_TOKEN:-$(new_token)}"

# The services talk over this machine only
export CHAT_BIND_HOST=127.0.0.1
export CHAT_SERVICE_PORT=50051
export CHAT_SERVICE_ADDRESS=127.0.0.1:50051
export INGEST_BIND_HOST=127.0.0.1
export INGEST_PORT=8100
export INGEST_SERVICE_URL=http://127.0.0.1:8100

pids=()

(cd /app/retriever && exec python main.py) &
pids+=($!)

(cd /app/ingestion && exec python main.py) &
pids+=($!)

(cd /app/ingestion && exec python worker.py) &
pids+=($!)

# Render says which port to use in $PORT
(cd /app/orchestrator && exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}") &
pids+=($!)

stopping=0
stop_all() {
    kill -TERM "${pids[@]}" 2>/dev/null
}
trap 'stopping=1; stop_all' TERM INT

wait -n "${pids[@]}"
status=$?

stop_all
wait

# A requested stop is a success. A process that ended on its own is a failure,
# even if it reported success.
if [ "$stopping" -eq 1 ]; then
    exit 0
fi
[ "$status" -eq 0 ] && status=1
exit "$status"
