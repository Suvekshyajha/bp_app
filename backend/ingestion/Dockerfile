# Ingestion: the document API and the background worker, in one container.
# Build from the repository root:
#   docker build -f backend/ingestion/Dockerfile -t infomate-ingestion .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/models/huggingface \
    FASTEMBED_CACHE_PATH=/opt/models/fastembed

WORKDIR /app

# Dependencies first, so code changes do not reinstall them
COPY backend/ingestion/requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/shared_common /tmp/shared_common
RUN pip install /tmp/shared_common && rm -rf /tmp/shared_common

# Download the embedding models now, so a start does not have to
RUN python -c "from common.embeddings import warm_up; warm_up()"

COPY backend/ingestion ./
# Windows line endings would stop the script from running
RUN sed -i 's/\r$//' start.sh

# Do not run as root. /app must stay writable: ingest.db and uploads are in it.
# /data is where a volume can keep the database across rebuilds
RUN useradd --create-home appuser && mkdir /data && chown -R appuser /app /opt/models /data
USER appuser

# Use the models downloaded above and never call Hugging Face at run time.
# In a container the service must listen on all interfaces; it still refuses
# to start without INGEST_SERVICE_TOKEN.
ENV HF_HUB_OFFLINE=1 \
    INGEST_BIND_HOST=0.0.0.0 \
    INGEST_PORT=8100

EXPOSE 8100

CMD ["bash", "start.sh"]
