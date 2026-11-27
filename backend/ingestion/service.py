"""The ingestion gRPC service (proto/ingest.proto): upload, list, get, delete.

Never imports the embedding model or the worker: this process stays light.
Every lookup is by (user_id, document_id), so a document of another user is
answered exactly like a missing one (NOT_FOUND, same message).
"""

import functools
import hashlib
import json
import logging
import os
import re
import time
import uuid

import grpc

import auth
import ingest_pb2
import ingest_pb2_grpc
import limits
import qdrant_docs
import repository
from auth import Refused
from common.log import set_request_id
from parsers import ParseError, check_file_signature, source_type_from_filename

logger = logging.getLogger(__name__)

MAX_FILENAME_CHARS = 120

Code = grpc.StatusCode


def _not_found() -> Refused:
    return Refused(Code.NOT_FOUND, "Document not found.")


def _too_large() -> Refused:
    return Refused(Code.OUT_OF_RANGE, f"File is larger than {limits.MAX_UPLOAD_MB} MB.")


def _clean_filename(raw: str | None) -> str:
    """No folders and no control characters. (Cut to 120 characters later, after
    the extension has been read.)"""

    name = os.path.basename((raw or "").replace("\\", "/"))
    return re.sub(r"[\x00-\x1f\x7f]", "", name).strip()


def _chunking_summary(report_json: str | None):
    if not report_json:
        return None
    try:
        report = json.loads(report_json)
    except ValueError:
        return None
    return ingest_pb2.ChunkingSummary(
        strategy_used=report.get("used_strategy"),
        fallback_reason=report.get("fallback_reason") or "",
        chunk_count=report.get("chunk_count"),
    )


def _public(document: dict) -> ingest_pb2.Document:
    created = document["created_at"]
    return ingest_pb2.Document(
        id=document["id"],
        filename=document["filename"],
        status=document["status"],
        stage=document["stage"],  # None leaves an optional field unset
        error=document["error"],
        chunk_count=document["chunk_count"],
        created_at=created.isoformat() + "Z" if created else None,
        chunking=_chunking_summary(document["report"]),
    )


def _short(document: dict) -> ingest_pb2.Document:
    return ingest_pb2.Document(
        id=document["id"], filename=document["filename"], status=document["status"]
    )


def _remove(path) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _call(method):
    """Around every call: the request id, the caller check, one result line, and
    refusals turned into gRPC status codes."""

    name = method.__name__

    @functools.wraps(method)
    def wrapped(self, request, context):
        metadata = dict(context.invocation_metadata() or ())
        set_request_id(metadata.get("x-request-id"))
        started = time.monotonic()

        try:
            user_id = auth.require_user(metadata)
            response = method(self, request, user_id)

        except Refused as refusal:
            logger.warning(
                "%s refused: %s: %s (%d ms)",
                name, refusal.code.name, refusal.detail,
                int((time.monotonic() - started) * 1000),
            )
            context.abort(refusal.code, refusal.detail)

        except Exception:
            logger.exception("%s crashed", name)
            context.abort(Code.INTERNAL, "Internal error.")

        logger.info("%s -> OK (%d ms)", name, int((time.monotonic() - started) * 1000))
        return response

    return wrapped


class DocumentService(ingest_pb2_grpc.DocumentServiceServicer):

    @_call
    def Upload(self, requests, user_id: str):
        first = next(requests, None)
        if first is None or first.WhichOneof("part") != "info":
            raise Refused(Code.INVALID_ARGUMENT, "The file name must be sent first.")

        full_name = _clean_filename(first.info.filename)
        source_type = source_type_from_filename(full_name)
        if not full_name or source_type is None:
            logger.warning(f"Upload refused: unsupported or missing file type for {full_name or '(no name)'}.")
            raise Refused(Code.INVALID_ARGUMENT, "Only .pdf, .docx, .txt and .md files are accepted.")
        filename = full_name[:MAX_FILENAME_CHARS]
        logger.debug(f"Upload received: {filename} (type {source_type}).")

        limits.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        temporary = limits.UPLOAD_DIR / f".tmp-{uuid.uuid4().hex}"

        try:
            # Copy while counting bytes and hashing; stop as soon as it is too big
            digest = hashlib.sha256()
            size = 0
            with open(temporary, "wb") as out:
                for message in requests:
                    block = message.chunk
                    size += len(block)
                    if size > limits.MAX_UPLOAD_BYTES:
                        logger.warning(f"Upload refused: {filename} is larger than {limits.MAX_UPLOAD_MB} MB.")
                        raise _too_large()
                    digest.update(block)
                    out.write(block)

            if size == 0:
                logger.warning(f"Upload refused: {filename} is empty.")
                raise Refused(Code.INVALID_ARGUMENT, "The file is empty.")

            logger.debug(f"Copied {filename} to a temporary file ({size} bytes).")

            try:
                check_file_signature(str(temporary), source_type)
            except ParseError as error:
                if error.code == limits.ERR_TOO_LARGE:
                    logger.warning(f"Upload refused: {filename} has too much text.")
                    raise Refused(Code.OUT_OF_RANGE, "The file has too much text.") from None
                logger.warning(f"File signature check failed for {filename} (expected {source_type}, code {error.code}).")
                raise Refused(Code.INVALID_ARGUMENT, "The file content does not match its type.") from None

            logger.debug(f"File signature check passed for {filename}.")

            file_hash = digest.hexdigest()
            document_id = repository.make_document_id(user_id, file_hash)

            # The same user and the same file again: nothing new, unless it had failed
            existing = repository.get_document(user_id, document_id)
            if existing is not None and existing["status"] == limits.FAILED:
                logger.info(f"Re-uploading {filename}: document {document_id} had failed before.")

            if existing is not None and existing["status"] != limits.FAILED:
                logger.info(f"Duplicate upload of {filename}: document {document_id} is already {existing['status']}.")
                return ingest_pb2.UploadResponse(document=_public(existing), existing=True)

            if existing is None and repository.count_user_documents(user_id) >= limits.MAX_DOCS_PER_USER:
                logger.warning(f"Upload refused: document limit ({limits.MAX_DOCS_PER_USER}) reached for {filename}.")
                raise Refused(
                    Code.FAILED_PRECONDITION,
                    f"You can keep at most {limits.MAX_DOCS_PER_USER} documents. Delete one first.",
                )

            # The file goes in place BEFORE the row exists, so the worker never
            # picks up a job whose file is missing
            os.replace(temporary, limits.UPLOAD_DIR / document_id)

            document, created = repository.create_document(user_id, file_hash, filename, source_type)
            if not created and document["status"] not in (limits.PENDING, limits.PROCESSING):
                logger.debug(f"Upload of {filename} lost a race with a finished upload of document {document_id}.")
                _remove(limits.UPLOAD_DIR / document_id)  # lost a race with a finished upload
                return ingest_pb2.UploadResponse(document=_public(document), existing=True)

            logger.info(f"Document {document['id']} ({filename}, {size} bytes) {'queued for processing' if created else 'already in progress'}.")
            return ingest_pb2.UploadResponse(document=_short(document), queued=created)

        finally:
            _remove(temporary)

    @_call
    def ListDocuments(self, request, user_id: str):
        return ingest_pb2.ListDocumentsResponse(
            documents=[_public(document) for document in repository.list_documents(user_id)]
        )

    @_call
    def GetDocument(self, request, user_id: str):
        document = repository.get_document(user_id, request.document_id)
        if document is None:
            raise _not_found()
        return _public(document)

    @_call
    def DeleteDocument(self, request, user_id: str):
        document_id = request.document_id

        if repository.get_document(user_id, document_id) is None:
            raise _not_found()

        # Same order as deleting a chat: mark, chunks, then the row
        repository.mark_deleting(user_id, document_id)

        try:
            qdrant_docs.delete_document(user_id, document_id)
        except Exception as error:
            logger.error(f"Could not delete chunks of document {document_id}: {type(error).__name__}")
            raise Refused(
                Code.UNAVAILABLE,
                "The search index is not reachable. Try again in a moment.",
            ) from None

        _remove(limits.UPLOAD_DIR / document_id)
        repository.delete_row(document_id)
        logger.info(f"Deleted document {document_id}.")
        return ingest_pb2.DeleteDocumentResponse()
