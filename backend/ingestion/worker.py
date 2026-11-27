"""The background worker. Run it as its own process:  python worker.py

It takes one pending job at a time, so only one document uses the embedding
API at once. Imports are inside main() on purpose: the parser starts child
processes with "spawn", and a child re-imports this file, which must stay cheap.
"""

import logging
import os
import signal
import time

POLL_SECONDS = 1.0
RECOVER_EVERY_SECONDS = 60.0


def recover_stale() -> int:
    """Put jobs of a crashed worker back in the queue."""

    import limits
    import repository

    return repository.requeue_stale(limits.JOB_TIMEOUT_SECONDS * 2)


def run_once() -> bool:
    """Take and run one job. False when there was nothing to do."""

    import jobs
    import repository

    claimed = repository.claim_next_job()
    if claimed is None:
        return False

    logging.getLogger("worker").info("Claimed document=%s", claimed["id"])  # [log]
    jobs.process_document(claimed["id"])
    return True


def main() -> None:
    from common.log import setup_logging

    setup_logging("ingestion-worker")
    logger = logging.getLogger("worker")

    import limits
    import qdrant_docs
    from database import Base, engine, migrate_schema
    import models  # noqa: F401  (registers the tables)

    # Be gentle with the machine: the chat service must stay responsive
    if hasattr(os, "nice"):
        try:
            os.nice(10)
        except OSError:
            pass

    limits.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    migrate_schema()
    qdrant_docs.ensure_collection()

    from common.embeddings import warm_up

    logger.info("Loading models...")
    warm_up()

    stop = False

    def request_stop(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    logger.info("Worker ready.")
    last_recovery = 0.0

    while not stop:
        if time.monotonic() - last_recovery >= RECOVER_EVERY_SECONDS:
            last_recovery = time.monotonic()
            count = recover_stale()
            if count:
                logger.warning("Put %d stale job(s) back in the queue.", count)

        try:
            worked = run_once()
        except Exception as error:
            logger.error("Worker loop error: %s", type(error).__name__)
            worked = False

        if not worked:
            time.sleep(POLL_SECONDS)

    logger.info("Worker stopped.")


if __name__ == "__main__":
    main()
