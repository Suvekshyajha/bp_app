# Orchestrator: the public API and WebSocket.
# Build from the repository root:
#   docker build -f backend/orchestrator/Dockerfile -t infomate-orchestrator .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so code changes do not reinstall them
COPY backend/orchestrator/requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/shared_common /tmp/shared_common
RUN pip install /tmp/shared_common && rm -rf /tmp/shared_common

COPY backend/orchestrator ./
COPY backend/proto /tmp/proto
# The gRPC code is not stored in Git, so it is generated here
RUN python -m grpc_tools.protoc -I /tmp/proto \
        --python_out=. --grpc_python_out=. \
        /tmp/proto/chat.proto /tmp/proto/ingest.proto \
    && rm -rf /tmp/proto

# Do not run as root. /app must stay writable: chat.db is created in it.
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Hosts such as Render say which port to use in $PORT
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
