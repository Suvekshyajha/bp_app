"""One ingestion job, start to finish (plan 8).

parse (child process) -> clean -> stats -> stage 1 analysis -> stage 2 chunking
-> embed -> store -> mark ready.

Rules this file keeps:
- A failure is a short safe code in the database, never a stack trace or any
  document text, and the log line has no document text either.
- Chunks become searchable only at the very end, in one call (mark_ready).
- If the user deletes the document while it is processing, the job notices,
  removes what it stored and stops, and never writes `ready` over `deleting`.
- On any failure the partly stored chunks are removed.
"""

import json
import logging
import os
import time

import limits
import qdrant_docs
import repository
from analyzer import analyze
from chunking import run_chunking
from chunking.validate import ChunkingError
from common.sanitize import strip_hidden_characters
from doc_model import ParsedDocument, assign_section_paths
from parsers import ParseError, parse_in_child
from stats import compute_stats

logger = logging.getLogger(__name__)


def _embed(texts: list[str]) -> list[dict]:
    # Imported here so the API process and the tests never load a model
    from common.embeddings import embed_passages

    return embed_passages(texts)


def _file_path(document_id: str) -> str:
    return str(limits.UPLOAD_DIR / document_id)


def _remove_file(document_id: str) -> None:
    try:
        os.remove(_file_path(document_id))
    except FileNotFoundError:
        pass
    except OSError as error:
        logger.warning("Could not remove an upload: %s", type(error).__name__)


def _discard_chunks(user_id: str, document_id: str) -> None:
    try:
        qdrant_docs.delete_document(user_id, document_id)
    except Exception as error:
        logger.warning("Could not remove chunks: %s", type(error).__name__)


def _clean(parsed: ParsedDocument) -> ParsedDocument:
    """Strip hidden characters and drop blocks that end up empty."""

    kept = []
    for block in parsed.blocks:
        block.text = strip_hidden_characters(block.text).strip()
        if block.text:
            kept.append(block)

    parsed.blocks = kept
    assign_section_paths(kept)
    return parsed


def _embedding_text(chunk) -> str:
    return f"{chunk.section_title}\n{chunk.text}" if chunk.section_title else chunk.text


def _is_processing(document_id: str) -> bool:
    document = repository.get_document_by_id(document_id)
    return document is not None and document["status"] == limits.PROCESSING


def process_document(document_id: str) -> None:
    """Run one claimed job. Never raises: every outcome is written to the database."""

    document = repository.get_document_by_id(document_id)
    if document is None or document["status"] != limits.PROCESSING:
        return

    user_id = document["user_id"]
    timings: dict[str, int] = {}
    started = time.monotonic()
    outcome = "ready"

    def lap(name: str, since: float) -> float:
        now = time.monotonic()
        timings[name] = int((now - since) * 1000)
        return now

    try:
        # 1. parse, in a child process with a timeout
        repository.set_stage(document_id, "parsing")
        mark = time.monotonic()
        parsed = parse_in_child(_file_path(document_id), document["source_type"])
        parsed = _clean(parsed)
        if not parsed.blocks:
            raise ParseError(limits.ERR_NO_TEXT)
        stats = compute_stats(parsed)
        mark = lap("parse_ms", mark)

        # 2. stage 1: decide the strategy (reuses an earlier LLM answer)
        repository.set_stage(document_id, "analyzing")
        cached = None
        if document["analysis"]:
            try:
                cached = json.loads(document["analysis"])
            except ValueError:
                cached = None
        analysis = analyze(parsed, stats, cached=cached)
        repository.save_analysis(document_id, json.dumps(analysis.to_dict()))
        mark = lap("analyze_ms", mark)

        # 3. stage 2: chunk, deterministic
        repository.set_stage(document_id, "chunking")
        result = run_chunking(parsed, analysis)
        mark = lap("chunk_ms", mark)

        if not _is_processing(document_id):
            raise _Deleted()

        # 4. start clean (a retry may have left chunks behind), then embed and store
        _discard_chunks(user_id, document_id)
        repository.set_stage(document_id, "embedding")
        embed_ms = 0
        store_ms = 0
        for first in range(0, len(result.chunks), limits.EMBED_BATCH_SIZE):
            batch = result.chunks[first : first + limits.EMBED_BATCH_SIZE]

            tick = time.monotonic()
            vectors = _embed([_embedding_text(chunk) for chunk in batch])
            embed_ms += int((time.monotonic() - tick) * 1000)

            tick = time.monotonic()
            qdrant_docs.upsert_chunks(
                user_id, document_id, document["file_hash"], document["filename"],
                result.report["used_strategy"], batch, vectors,
            )
            store_ms += int((time.monotonic() - tick) * 1000)

            if not _is_processing(document_id):
                raise _Deleted()
        timings["embed_ms"], timings["store_ms"] = embed_ms, store_ms

        # 5. the last steps: report, searchable, ready
        repository.set_stage(document_id, "storing")
        timings["total_ms"] = int((time.monotonic() - started) * 1000)
        repository.save_report(document_id, json.dumps({**result.report, "timings": timings}))

        qdrant_docs.mark_ready(user_id, document_id)
        if not repository.set_ready(document_id, len(result.chunks)):
            raise _Deleted()  # deleted at the last moment

    except _Deleted:
        outcome = "deleted"
        _discard_chunks(user_id, document_id)

    except (ParseError, ChunkingError) as error:
        outcome = error.code
        repository.set_failed(document_id, error.code)
        _discard_chunks(user_id, document_id)

    except Exception as error:
        # Only the type is logged: messages can contain document text
        logger.error("Job failed unexpectedly: %s", type(error).__name__)
        outcome = limits.ERR_UNSUPPORTED
        repository.set_failed(document_id, limits.ERR_UNSUPPORTED)
        _discard_chunks(user_id, document_id)

    finally:
        _remove_file(document_id)

    logger.info(
        "job document=%s outcome=%s total_ms=%d",
        document_id, outcome, int((time.monotonic() - started) * 1000),
    )


class _Deleted(Exception):
    """The user deleted the document while its job ran."""
