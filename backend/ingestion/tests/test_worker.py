import pytest

import jobs
import repository
import worker


@pytest.fixture(autouse=True)
def fresh(session_factory):
    yield


def test_run_once_with_nothing_to_do_returns_false():
    assert worker.run_once() is False


def test_run_once_claims_and_processes_one_job(monkeypatch):
    seen = []
    monkeypatch.setattr(jobs, "process_document", lambda document_id: seen.append(document_id))
    first, _ = repository.create_document("u1", "a" * 64, "a.txt", "txt")
    repository.create_document("u1", "b" * 64, "b.txt", "txt")

    assert worker.run_once() is True

    assert seen == [first["id"]]  # the oldest one, once
    assert repository.get_document_by_id(first["id"])["status"] == "processing"


def test_two_claims_never_take_the_same_job():
    repository.create_document("u1", "a" * 64, "a.txt", "txt")

    first = repository.claim_next_job()
    second = repository.claim_next_job()

    assert first is not None and second is None


def test_recover_stale_puts_old_processing_jobs_back(monkeypatch):
    import limits
    from datetime import timedelta
    from database import SessionLocal
    from models import Document

    document, _ = repository.create_document("u1", "a" * 64, "a.txt", "txt")
    repository.claim_next_job()
    assert worker.recover_stale() == 0

    with SessionLocal() as db:
        row = db.get(Document, document["id"])
        row.started_at = row.started_at - timedelta(seconds=limits.JOB_TIMEOUT_SECONDS * 3)
        db.commit()

    assert worker.recover_stale() == 1
    assert repository.get_document_by_id(document["id"])["status"] == "pending"
