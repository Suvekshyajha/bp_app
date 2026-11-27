# The whole backend in one container, for a single Render web service.
# Build from the repository root:
#   docker build -f backend/render/Dockerfile -t infomate-backend .
FROM python:3.12-slim

# MALLOC_ARENA_MAX keeps memory use down: four processes share one small machine
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MALLOC_ARENA_MAX=2 \
    HF_HOME=/opt/models/huggingface \
    FASTEMBED_CACHE_PATH=/opt/models/fastembed

WORKDIR /app

# Dependencies first, so code changes do not reinstall them
COPY backend/orchestrator/requirements.txt /tmp/requirements/orchestrator.txt
COPY backend/retriever/requirements.txt /tmp/requirements/retriever.txt
COPY backend/ingestion/requirements.txt /tmp/requirements/ingestion.txt
RUN pip install -r /tmp/requirements/orchestrator.txt \
        -r /tmp/requirements/retriever.txt \
        -r /tmp/requirements/ingestion.txt \
    && rm -rf /tmp/requirements

COPY backend/shared_common /tmp/shared_common
RUN pip install /tmp/shared_common && rm -rf /tmp/shared_common

# Download the keyword (BM25) model now, so a start does not have to
RUN python -c "from common.embeddings import get_sparse; get_sparse()"

# Each service keeps its own folder: they have files with the same names
COPY backend/orchestrator ./orchestrator
COPY backend/retriever ./retriever
COPY backend/ingestion ./ingestion
COPY backend/render/start.sh ./start.sh
COPY backend/proto /tmp/proto
# The gRPC code is not stored in Git, so it is generated here.
# The sed removes Windows line endings, which would stop the script.
RUN for pair in orchestrator:chat retriever:chat orchestrator:ingest ingestion:ingest; do \
        service=${pair%:*}; \
        python -m grpc_tools.protoc -I /tmp/proto \
            --python_out=$service --grpc_python_out=$service /tmp/proto/${pair#*:}.proto || exit 1; \
    done \
    && rm -rf /tmp/proto \
    && sed -i 's/\r$//' start.sh

# Do not run as root. The databases and uploads are kept in /data, where a
# disk can be mounted to keep them across restarts.
RUN useradd --create-home appuser && mkdir /data && chown -R appuser /opt/models /data
USER appuser

# Use the model downloaded above and never call Hugging Face at run time
ENV HF_HUB_OFFLINE=1 \
    ORCH_DB_PATH=/data/orchestrator.db \
    CHAT_DB_PATH=/data/chat.db \
    INGEST_DB_PATH=/data/ingest.db \
    INGEST_UPLOAD_DIR=/data/uploads

EXPOSE 8000

CMD ["bash", "start.sh"]
