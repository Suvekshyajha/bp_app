"""The ingestion HTTP API (plan 5): POST, GET and DELETE /documents.

Never imports the embedding model or the worker: the API process stays light.
Every lookup is by (user_id, document_id), so a document of another user is
answered exactly like a missing one (404, same body).
"""

import hashlib
import json
import logging
import os
import re
import uuid
import logging

logger = logging.getLogger(name=__name__)

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse

import auth
import limits
import qdrant_docs
import repository
from parsers import ParseError, check_file_signature, source_type_from_filename

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents")

MAX_FILENAME_CHARS = 120
READ_BLOCK = 1024 * 1024
MULTIPART_OVERHEAD = 1024 * 1024  # headers and boundaries around the file

NOT_FOUND = HTTPException(status_code=404, detail="Document not found.")


def _clean_filename(raw: str | None) -> str:
    """No folders and no control characters. (Cut to 120 characters later, after
    the extension has been read.)"""

    name = os.path.basename((raw or "").replace("\\", "/"))
    return re.sub(r"[\x00-\x1f\x7f]", "", name).strip()


def _chunking_summary(report_json: str | None) -> dict | None:
    if not report_json:
        return None
    try:
        report = json.loads(report_json)
    except ValueError:
        return None
    return {
        "strategy_used": report.get("used_strategy"),
        "fallback_reason": report.get("fallback_reason", ""),
        "chunk_count": report.get("chunk_count"),
    }


def _public(document: dict) -> dict:
    created = document["created_at"]
    return {
        "id": document["id"],
        "filename": document["filename"],
        "status": document["status"],
        "stage": document["stage"],
        "error": document["error"],
        "chunk_count": document["chunk_count"],
        "created_at": created.isoformat() + "Z" if created else None,
        "chunking": _chunking_summary(document["report"]),
    }


def _remove(path) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


@router.post("")
def upload(request: Request, file: UploadFile = File(...), user_id: str = Depends(auth.require_user)):
    # Cheap early refusal, before anything is copied
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limits.MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD:
        logger.warning("Upload refused: status=%s detail=%s user=%s file=%s size=%s", 413, f"File is larger than {limits.MAX_UPLOAD_MB} MB.", user_id, locals().get('full_name', '-'), locals().get('size', '-'))  # [log]
        raise HTTPException(status_code=413, detail=f"File is larger than {limits.MAX_UPLOAD_MB} MB.")

    full_name = _clean_filename(file.filename)
    source_type = source_type_from_filename(full_name)
    if not full_name or source_type is None:
        logger.warning("Upload refused: status=%s detail=%s user=%s file=%s size=%s", 415, "Only .pdf, .docx, .txt and .md files are accepted.", user_id, locals().get('full_name', '-'), locals().get('size', '-'))  # [log]
        raise HTTPException(status_code=415, detail="Only .pdf, .docx, .txt and .md files are accepted.")
    filename = full_name[:MAX_FILENAME_CHARS]

    limits.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    temporary = limits.UPLOAD_DIR / f".tmp-{uuid.uuid4().hex}"

    try:
        # Copy while counting bytes and hashing; stop as soon as it is too big
        digest = hashlib.sha256()
        size = 0
        with open(temporary, "wb") as out:
            while True:
                block = file.file.read(READ_BLOCK)
                if not block:
                    break
                size += len(block)
                if size > limits.MAX_UPLOAD_BYTES:
                    logger.warning("Upload refused: status=%s detail=%s user=%s file=%s size=%s", 413, f"File is larger than {limits.MAX_UPLOAD_MB} MB.", user_id, locals().get('full_name', '-'), locals().get('size', '-'))  # [log]
                    raise HTTPException(status_code=413, detail=f"File is larger than {limits.MAX_UPLOAD_MB} MB.")
                digest.update(block)
                out.write(block)
                logger.debug(f"Read {len(block)} bytes from uploaded file.")

        if size == 0:
            logger.warning("Upload refused: status=%s detail=%s user=%s file=%s size=%s", 415, "The file is empty.", user_id, locals().get('full_name', '-'), locals().get('size', '-'))  # [log]
            raise HTTPException(status_code=415, detail="The file is empty.")

        try:
            check_file_signature(str(temporary), source_type)
        except ParseError as error:
            if error.code == limits.ERR_TOO_LARGE:
                logger.warning("File %s rejected: %s", filename, error) 
                logger.warning("Upload refused: status=%s detail=%s user=%s file=%s size=%s", 413, "The file has too much text.", user_id, locals().get('full_name', '-'), locals().get('size', '-'))  # [log]
                raise HTTPException(status_code=413, detail="The file has too much text.") from None
          
            logger.warning("Upload refused: status=%s detail=%s user=%s file=%s size=%s", 415, "The file content does not match its type.", user_id, locals().get('full_name', '-'), locals().get('size', '-'))  # [log]
            raise HTTPException(status_code=415, detail="The file content does not match its type.") from None

        file_hash = digest.hexdigest()
        document_id = repository.make_document_id(user_id, file_hash)

        # The same user and the same file again: nothing new, unless it had failed
        existing = repository.get_document(user_id, document_id)
        if existing is not None and existing["status"] != limits.FAILED:
            logger.info("Upload duplicate: user=%s document=%s", user_id, document_id)  # [log]
            return JSONResponse(status_code=200, content=_public(existing))

        if existing is None and repository.count_user_documents(user_id) >= limits.MAX_DOCS_PER_USER:
            logger.warning("Upload refused: status=%s detail=%s user=%s file=%s size=%s", 409, f"You can keep at most {limits.MAX_DOCS_PER_USER} documents. Delete one first.", user_id, locals().get('full_name', '-'), locals().get('size', '-'))  # [log]
            raise HTTPException(
                status_code=409,
                detail=f"You can keep at most {limits.MAX_DOCS_PER_USER} documents. Delete one first.",
            )

        # The file goes in place BEFORE the row exists, so the worker never
        # picks up a job whose file is missing
        os.replace(temporary, limits.UPLOAD_DIR / document_id)

        document, created = repository.create_document(user_id, file_hash, filename, source_type)
        if not created and document["status"] not in (limits.PENDING, limits.PROCESSING):
            _remove(limits.UPLOAD_DIR / document_id)  # lost a race with a finished upload
            return JSONResponse(status_code=200, content=_public(document))

        logger.info("Upload stored: user=%s document=%s file=%s size=%d created=%s", user_id, document["id"], filename, size, created)  # [log]
        status_code = 202 if created else 200
        return JSONResponse(
            status_code=status_code,
            content={"id": document["id"], "filename": document["filename"], "status": document["status"]},
        )

    finally:
        _remove(temporary)


@router.get("")
def list_documents(user_id: str = Depends(auth.require_user)):
    return [_public(document) for document in repository.list_documents(user_id)]


@router.get("/{document_id}")
def get_document(document_id: str, user_id: str = Depends(auth.require_user)):
    document = repository.get_document(user_id, document_id)
    if document is None:
        raise NOT_FOUND
    return _public(document)


@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: str, user_id: str = Depends(auth.require_user)):
    logger.info("Delete requested: user=%s document=%s", user_id, document_id)  # [log]
    if repository.get_document(user_id, document_id) is None:
        raise NOT_FOUND

    # Same order as deleting a chat: mark, chunks, then the row
    repository.mark_deleting(user_id, document_id)

    try:
        qdrant_docs.delete_document(user_id, document_id)
    except Exception as error:
        logger.error("Could not delete chunks: %s", type(error).__name__)
        raise HTTPException(
            status_code=503,
            detail="The search index is not reachable. Try again in a moment.",
        ) from None

    _remove(limits.UPLOAD_DIR / document_id)
    repository.delete_row(document_id)
    return Response(status_code=204)
