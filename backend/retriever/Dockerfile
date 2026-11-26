# Retriever: the gRPC chat service (search, prompt, model, history).
# Build from the repository root:
#   docker build -f backend/retriever/Dockerfile -t infomate-retriever .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/models/huggingface \
    FASTEMBED_CACHE_PATH=/opt/models/fastembed

WORKDIR /app

# Dependencies first, so code changes do not reinstall them
COPY backend/retriever/requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/shared_common /tmp/shared_common
RUN pip install /tmp/shared_common && rm -rf /tmp/shared_common

# Download the embedding models now, so a start does not have to
RUN python -c "from common.embeddings import warm_up; warm_up()"

COPY backend/retriever ./
COPY backend/proto /tmp/proto
# The gRPC code is not stored in Git, so it is generated here
RUN python -m grpc_tools.protoc -I /tmp/proto \
        --python_out=. --grpc_python_out=. /tmp/proto/chat.proto \
    && rm -rf /tmp/proto

# Do not run as root. /app must stay writable: chat.db is created in it.
RUN useradd --create-home appuser && chown -R appuser /app /opt/models
USER appuser

# Use the models downloaded above and never call Hugging Face at run time.
# In a container the service must listen on all interfaces; it still refuses
# to start without CHAT_SERVICE_TOKEN.
ENV HF_HUB_OFFLINE=1 \
    CHAT_BIND_HOST=0.0.0.0

EXPOSE 50051

CMD ["python", "main.py"]
