"""Everything that reads or writes ingest.db.

These are plain synchronous functions, each with its own short-lived session.
Callers inside the event loop call them with `asyncio.to_thread(...)`.

Every function that takes a user_id filters by it, so one user can never read
or change another user's document. Updates made by the worker only apply while
the row is still in the expected status, so a delete that happens in the
middle of a job is never overwritten.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from common.settings import DOC_NAMESPACE
from database import SessionLocal
from limits import DELETING, FAILED, PENDING, PROCESSING, READY
from models import Document


def make_document_id(user_id: str, file_hash: str) -> str:
    """The same user and the same file always give the same id."""

    return str(uuid.uuid5(DOC_NAMESPACE, f"{user_id}:{file_hash}"))


def _now() -> datetime:
    # SQLite keeps times without a timezone label, so everything is UTC
    # and naive, written and compared the same way.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_dict(row: Document) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "file_hash": row.file_hash,
        "filename": row.filename,
        "source_type": row.source_type,
        "status": row.status,
        "stage": row.stage,
        "error": row.error,
        "chunk_count": row.chunk_count or 0,
        "analysis": row.analysis,
        "report": row.report,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "started_at": row.started_at,
    }


# ---------- API side ----------

def create_document(
    user_id: str, file_hash: str, filename: str, source_type: str
) -> tuple[dict, bool]:
    """Add a `pending` document. Returns (document, created).

    Same user and same file again: nothing new is added and `created` is
    False (the upload is idempotent). A `failed` one is put back to `pending`
    and `created` is True, so the caller knows work was queued again.
    """

    document_id = make_document_id(user_id, file_hash)
    now = _now()

    with SessionLocal() as db:
        db.add(
            Document(
                id=document_id,
                user_id=user_id,
                file_hash=file_hash,
                filename=filename,
                source_type=source_type,
                status=PENDING,
                chunk_count=0,
                created_at=now,
                updated_at=now,
            )
        )

        try:
            db.commit()
            return _to_dict(db.get(Document, document_id)), True

        except IntegrityError:
            db.rollback()

        row = db.get(Document, document_id)
        if row is None or row.user_id != user_id:
            raise RuntimeError("Could not add the document.")

        if row.status == FAILED:
            row.status = PENDING
            row.stage = None
            row.error = None
            row.started_at = None
            row.updated_at = now
            db.commit()
            return _to_dict(row), True

        return _to_dict(row), False


def count_user_documents(user_id: str) -> int:
    with SessionLocal() as db:
        return db.execute(
            select(func.count()).select_from(Document).where(Document.user_id == user_id)
        ).scalar_one()


def get_document(user_id: str, document_id: str) -> dict | None:
    """One document, or None when it is missing OR belongs to someone else."""

    with SessionLocal() as db:
        row = db.execute(
            select(Document).where(
                Document.id == document_id, Document.user_id == user_id
            )
        ).scalar_one_or_none()
        return _to_dict(row) if row else None


def list_documents(user_id: str) -> list[dict]:
    with SessionLocal() as db:
        rows = db.execute(
            select(Document)
            .where(Document.user_id == user_id)
            .order_by(Document.created_at.desc(), Document.id)
        ).scalars()
        return [_to_dict(row) for row in rows]


def mark_deleting(user_id: str, document_id: str) -> bool:
    """First step of a delete. False when it is missing or not the user's."""

    with SessionLocal() as db:
        result = db.execute(
            update(Document)
            .where(Document.id == document_id, Document.user_id == user_id)
            .values(status=DELETING, updated_at=_now())
        )
        db.commit()
        return result.rowcount > 0


def delete_row(document_id: str) -> None:
    with SessionLocal() as db:
        row = db.get(Document, document_id)
        if row is not None:
            db.delete(row)
            db.commit()


# ---------- Worker side ----------

def get_document_by_id(document_id: str) -> dict | None:
    """For the worker only. It has no user, it works from a claimed job."""

    with SessionLocal() as db:
        row = db.get(Document, document_id)
        return _to_dict(row) if row else None


def claim_next_job() -> dict | None:
    """Take the oldest `pending` job, atomically.

    One UPDATE picks the row and marks it `processing`, so two workers can
    never take the same job.
    """

    inner = aliased(Document)
    oldest_pending = (
        select(inner.id)
        .where(inner.status == PENDING)
        .order_by(inner.created_at, inner.id)
        .limit(1)
        .scalar_subquery()
    )
    now = _now()

    with SessionLocal() as db:
        claimed = db.execute(
            update(Document)
            .where(Document.id == oldest_pending, Document.status == PENDING)
            .values(status=PROCESSING, started_at=now, updated_at=now)
            .returning(Document.id)
        ).scalar_one_or_none()
        db.commit()

        if claimed is None:
            return None

        return _to_dict(db.get(Document, claimed))


def requeue_stale(max_age_seconds: int) -> int:
    """Put `processing` jobs that started too long ago back to `pending`.

    This recovers jobs from a worker that crashed. Returns how many.
    """

    cutoff = _now() - timedelta(seconds=max_age_seconds)

    with SessionLocal() as db:
        result = db.execute(
            update(Document)
            .where(Document.status == PROCESSING, Document.started_at < cutoff)
            .values(status=PENDING, stage=None, started_at=None, updated_at=_now())
        )
        db.commit()
        return result.rowcount


def set_stage(document_id: str, stage: str) -> bool:
    """Show progress. Only applies while the job is still `processing`."""

    with SessionLocal() as db:
        result = db.execute(
            update(Document)
            .where(Document.id == document_id, Document.status == PROCESSING)
            .values(stage=stage, updated_at=_now())
        )
        db.commit()
        return result.rowcount > 0


def save_analysis(document_id: str, analysis_json: str) -> None:
    with SessionLocal() as db:
        db.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(analysis=analysis_json, updated_at=_now())
        )
        db.commit()


def save_report(document_id: str, report_json: str) -> None:
    with SessionLocal() as db:
        db.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(report=report_json, updated_at=_now())
        )
        db.commit()


def set_ready(document_id: str, chunk_count: int) -> bool:
    """Last step of a job. False if the document was deleted meanwhile."""

    with SessionLocal() as db:
        result = db.execute(
            update(Document)
            .where(Document.id == document_id, Document.status == PROCESSING)
            .values(
                status=READY,
                stage=None,
                error=None,
                chunk_count=chunk_count,
                updated_at=_now(),
            )
        )
        db.commit()
        return result.rowcount > 0


def set_failed(document_id: str, error_code: str) -> bool:
    """Record a safe error code. A `deleting` document is left alone."""

    with SessionLocal() as db:
        result = db.execute(
            update(Document)
            .where(
                Document.id == document_id,
                Document.status.in_([PENDING, PROCESSING]),
            )
            .values(status=FAILED, stage=None, error=error_code, updated_at=_now())
        )
        db.commit()
        return result.rowcount > 0
