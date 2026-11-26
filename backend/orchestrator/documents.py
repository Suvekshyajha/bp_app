"""Upload, list, look at and delete documents (REST, session cookie required).

The browser never says who it is: the user id is the session's user and is
what we send to the ingestion service. A `user_id` field in the form is
ignored. The service does its own checks too; these are the cheap early ones.
"""

import logging
import math
import os
import re
import tempfile
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile

import ingest_client
from auth import get_current_user
from ingest_client import IngestError
from models import User
from rate_limit import doc_upload_limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "10"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MULTIPART_OVERHEAD = 1024 * 1024  # headers and boundaries around the file
MAX_FILENAME_CHARS = 120
ALLOWED_EXTENSIONS = (".pdf", ".docx", ".txt", ".md")
READ_BLOCK = 1024 * 1024

# What the ingestion service said, passed on as it is. Anything else (a wrong
# service token, a crash) is OUR problem: it must never look like "you are
# logged out" to the browser, so it becomes 502.
PASS_THROUGH = {404, 409, 413, 415}


def to_http_error(error: IngestError) -> HTTPException:
    logger.warning("Document service error: status=%s detail=%s", error.status, error.detail)  # [log]
    if error.status in PASS_THROUGH:
        return HTTPException(status_code=error.status, detail=error.detail or "Request refused.")

    if error.status == 503:
        return HTTPException(
            status_code=503,
            detail="The document service is unavailable. Please try again shortly.",
        )

    logger.error("Ingestion service answered %s", error.status)
    return HTTPException(
        status_code=502,
        detail="The document service could not handle the request.",
    )


def clean_filename(raw: str | None) -> str:
    """No folders and no control characters."""

    name = os.path.basename((raw or "").replace("\\", "/"))
    return re.sub(r"[\x00-\x1f\x7f]", "", name).strip()


def checked_document_id(document_id: str) -> str:
    """A document id is a UUID. Anything else cannot exist, and is never
    placed into the URL we call."""

    try:
        return str(uuid.UUID(document_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="Document not found.") from None


@router.post("")
async def upload_document(request: Request, user: User = Depends(get_current_user)):
    # 1. The cheapest check first: a flood of uploads costs almost nothing
    allowed, retry_after = doc_upload_limiter.allow(user.id)

    if not allowed:
        wait = max(1, math.ceil(retry_after))
        logger.warning("Upload refused: status=%s detail=%s user=%s file=%s size=%s", 429, f"You are uploading too quickly. Please wait {wait} seconds and try again.", user.id, locals().get('full_name', '-'), locals().get('size', '-'))  # [log]
        raise HTTPException(
            status_code=429,
            detail=f"You are uploading too quickly. Please wait {wait} seconds and try again.",
            headers={"Retry-After": str(wait)},
        )

    # 2. Refuse a too-big body before reading any of it
    declared = request.headers.get("content-length")

    if not declared or not declared.isdigit():
        logger.warning("Upload refused: status=%s detail=%s user=%s file=%s size=%s", 411, "Content-Length is required.", user.id, locals().get('full_name', '-'), locals().get('size', '-'))  # [log]
        raise HTTPException(status_code=411, detail="Content-Length is required.")

    if int(declared) > MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD:
        logger.warning("Upload refused: status=%s detail=%s user=%s file=%s size=%s", 413, f"File is larger than {MAX_UPLOAD_MB} MB.", user.id, locals().get('full_name', '-'), locals().get('size', '-'))  # [log]
        raise HTTPException(status_code=413, detail=f"File is larger than {MAX_UPLOAD_MB} MB.")

    # 3. Read the form: one file, a few small fields (which we ignore)
    try:
        form = await request.form(max_files=1, max_fields=10)
    except HTTPException:
        raise
    except Exception:
        logger.warning("Upload refused: status=%s detail=%s user=%s file=%s size=%s", 400, "Could not read the upload.", user.id, locals().get('full_name', '-'), locals().get('size', '-'))  # [log]
        raise HTTPException(status_code=400, detail="Could not read the upload.") from None

    spool = tempfile.SpooledTemporaryFile(max_size=READ_BLOCK)

    try:
        upload = form.get("file")

        if not isinstance(upload, UploadFile):
            logger.warning("Upload refused: status=%s detail=%s user=%s file=%s size=%s", 400, "A file is required.", user.id, locals().get('full_name', '-'), locals().get('size', '-'))  # [log]
            raise HTTPException(status_code=400, detail="A file is required.")

        full_name = clean_filename(upload.filename)

        if not full_name.lower().endswith(ALLOWED_EXTENSIONS):
            logger.warning("Upload refused: status=%s detail=%s user=%s file=%s size=%s", 415, "Only .pdf, .docx, .txt and .md files are accepted.", user.id, locals().get('full_name', '-'), locals().get('size', '-'))  # [log]
            raise HTTPException(
                status_code=415,
                detail="Only .pdf, .docx, .txt and .md files are accepted.",
            )

        # 4. Count the bytes while copying; stop as soon as it is too big
        size = 0

        while True:
            block = await upload.read(READ_BLOCK)

            if not block:
                break

            size += len(block)

            if size > MAX_UPLOAD_BYTES:
                logger.warning("Upload refused: status=%s detail=%s user=%s file=%s size=%s", 413, f"File is larger than {MAX_UPLOAD_MB} MB.", user.id, locals().get('full_name', '-'), locals().get('size', '-'))  # [log]
                raise HTTPException(
                    status_code=413,
                    detail=f"File is larger than {MAX_UPLOAD_MB} MB.",
                )

            spool.write(block)

        if size == 0:
            logger.warning("Upload refused: status=%s detail=%s user=%s file=%s size=%s", 415, "The file is empty.", user.id, locals().get('full_name', '-'), locals().get('size', '-'))  # [log]
            raise HTTPException(status_code=415, detail="The file is empty.")

        spool.seek(0)

        # 5. The user id is the session's, never the form's
        try:
            status_code, body = await ingest_client.upload(
                user.id,
                full_name[:MAX_FILENAME_CHARS],
                spool,
            )
        except IngestError as error:
            raise to_http_error(error) from None

        logger.info("Upload accepted: user=%s file=%s size=%s status=%s", user.id, full_name, size, status_code)  # [log]
        return JSONResponse(status_code=status_code, content=body)

    finally:
        spool.close()
        await form.close()


@router.get("")
async def list_documents(user: User = Depends(get_current_user)):
    try:
        return await ingest_client.list_documents(user.id)
    except IngestError as error:
        raise to_http_error(error) from None


@router.get("/{document_id}")
async def get_document(document_id: str, user: User = Depends(get_current_user)):
    document_id = checked_document_id(document_id)

    try:
        return await ingest_client.get_document(user.id, document_id)
    except IngestError as error:
        raise to_http_error(error) from None


@router.delete("/{document_id}", status_code=204)
async def delete_document(document_id: str, user: User = Depends(get_current_user)):
    logger.info("Delete requested: user=%s document=%s", user.id, document_id)  # [log]
    document_id = checked_document_id(document_id)

    try:
        await ingest_client.delete_document(user.id, document_id)
    except IngestError as error:
        raise to_http_error(error) from None

    return Response(status_code=204)
