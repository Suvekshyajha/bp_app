from datetime import datetime, timezone

from sqlalchemy import Column, Index, Integer, String, UniqueConstraint

from common.crypto import EncryptedText
from common.database import UTCDateTime
from database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    """One uploaded file. The job state lives in `status` and `stage`."""

    __tablename__ = "documents"

    # uuid5 of "user_id:file_hash" (see repository.make_document_id)
    id = Column(String, primary_key=True)

    # No ForeignKey: users live in the orchestrator's database.
    user_id = Column(String, nullable=False)
    file_hash = Column(String(64), nullable=False)  # sha256 hex

    # Encrypted at rest (see common/crypto.py)
    filename = Column(EncryptedText, nullable=False)

    source_type = Column(String, nullable=False)  # pdf, docx, txt, md

    # pending, processing, ready, failed, deleting
    status = Column(String, nullable=False, default="pending")

    # parsing, analyzing, chunking, embedding, storing (progress for the UI)
    stage = Column(String, nullable=True)

    # A short safe code only, never a stack trace
    error = Column(String, nullable=True)

    chunk_count = Column(Integer, nullable=False, default=0)

    # Both describe the document, so both are encrypted at rest.
    # analysis: the validated analysis JSON (the cache, decision D9)
    # report: the chunking report JSON
    analysis = Column(EncryptedText, nullable=True)
    report = Column(EncryptedText, nullable=True)

    created_at = Column(UTCDateTime, default=_utcnow)
    updated_at = Column(UTCDateTime, default=_utcnow)
    started_at = Column(UTCDateTime, nullable=True)

    __table_args__ = (
        # The same user uploading the same file again finds this row
        UniqueConstraint("user_id", "file_hash", name="uq_documents_user_hash"),
        # The worker's "oldest pending job" query
        Index("ix_documents_status_created", "status", "created_at"),
        # Lists a user's documents
        Index("ix_documents_user", "user_id"),
    )
