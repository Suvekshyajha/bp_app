"""The ingestion gRPC server. Start it with:  python main.py

The background worker is a separate process (python worker.py).
"""

import logging
import os
import signal
from concurrent.futures import ThreadPoolExecutor

import grpc

import auth
import ingest_pb2_grpc
import limits
import models  # noqa: F401  (registers the tables)
from database import Base, engine, migrate_schema
from common.database import create_schema
from common.log import setup_logging
from service import DocumentService

logger = logging.getLogger(__name__)

# Calls handled at the same time (an upload holds one while the file arrives)
MAX_WORKERS = 10


def _read(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def build_server(address: str) -> tuple[grpc.Server, int]:
    """A server listening on `address`, not yet started. Returns it and the port.

    Refuses to start without a service token (see common/service_auth.py), and
    prepares the upload folder and the database.
    """

    auth.init_from_env()

    limits.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    create_schema(engine, Base.metadata)
    migrate_schema()

    server = grpc.server(ThreadPoolExecutor(max_workers=MAX_WORKERS))
    ingest_pb2_grpc.add_DocumentServiceServicer_to_server(DocumentService(), server)

    # Optional TLS (file paths). Without it the port is plain text, which is
    # only acceptable on a trusted internal network.
    certificate = os.getenv("INGEST_TLS_CERT")
    key = os.getenv("INGEST_TLS_KEY")

    if bool(certificate) != bool(key):
        raise RuntimeError("Set both INGEST_TLS_CERT and INGEST_TLS_KEY, or neither.")

    if certificate:
        credentials = grpc.ssl_server_credentials([(_read(key), _read(certificate))])
        port = server.add_secure_port(address, credentials)
    else:
        logger.warning("Listening on %s WITHOUT TLS", address)
        port = server.add_insecure_port(address)

    if not port:
        raise RuntimeError(f"Could not listen on {address}. Is the port in use?")

    return server, port


def serve() -> None:
    host = os.getenv("INGEST_BIND_HOST", "127.0.0.1")
    port = int(os.getenv("INGEST_PORT", "8100"))

    server, _ = build_server(f"{host}:{port}")
    server.start()
    logger.info("Ingestion service running on %s:%d (gRPC)", host, port)

    # A requested stop lets running calls finish
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: server.stop(grace=5))

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(grace=5).wait()


if __name__ == "__main__":
    setup_logging("ingestion")
    serve()
