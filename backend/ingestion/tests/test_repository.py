from datetime import timedelta

import pytest
from sqlalchemy import text

import repository
from database import engine
from limits import DELETING, FAILED, PENDING, PROCESSING, READY

HASH_A = "a" * 64
HASH_B = "b" * 64


def add(user="user-1", file_hash=HASH_A, name="report.pdf", kind="pdf"):
    return repository.create_document(user, file_hash, name, kind)


def test_a_new_document_is_pending(session_factory):
    document, created = add()

    assert created is True
    assert document["status"] == PENDING
    assert document["filename"] == "report.pdf"
    assert document["chunk_count"] == 0
    assert document["id"] == repository.make_document_id("user-1", HASH_A)


def test_the_same_user_and_file_again_is_one_document(session_factory):
    first, created_first = add()
    second, created_second = add()

    assert created_first is True
    assert created_second is False
    assert first["id"] == second["id"]
    assert repository.count_user_documents("user-1") == 1


def test_two_users_with_the_same_file_get_two_documents(session_factory):
    mine, _ = add(user="user-1")
    theirs, _ = add(user="user-2")

    assert mine["id"] != theirs["id"]
    assert repository.count_user_documents("user-1") == 1
    assert repository.count_user_documents("user-2") == 1


def test_a_failed_document_is_queued_again(session_factory):
    document, _ = add()
    repository.claim_next_job()
    repository.set_failed(document["id"], "timeout")

    again, created = add()

    assert created is True
    assert again["status"] == PENDING
    assert again["error"] is None


def test_get_and_list_only_show_the_owners_documents(session_factory):
    mine, _ = add(user="user-1")
    add(user="user-2", file_hash=HASH_B)

    assert repository.get_document("user-1", mine["id"])["id"] == mine["id"]
    assert repository.get_document("user-2", mine["id"]) is None  # someone else's
    assert repository.get_document("user-1", "missing") is None
    assert [d["id"] for d in repository.list_documents("user-1")] == [mine["id"]]


def test_filename_and_analysis_are_encrypted_in_the_file(session_factory):
    document, _ = add(name="secret-plan.pdf")
    repository.save_analysis(document["id"], '{"strategy": "recursive"}')
    repository.save_report(document["id"], '{"chunk_count": 3}')

    with engine.connect() as connection:
        raw = connection.execute(
            text("SELECT filename, analysis, report FROM documents")
        ).one()

    assert "secret-plan" not in raw[0]
    assert "recursive" not in raw[1]
    assert "chunk_count" not in raw[2]

    again = repository.get_document("user-1", document["id"])
    assert again["filename"] == "secret-plan.pdf"
    assert again["analysis"] == '{"strategy": "recursive"}'
    assert again["report"] == '{"chunk_count": 3}'


def test_claim_takes_the_oldest_pending_job_once(session_factory):
    first, _ = add(file_hash=HASH_A)
    second, _ = add(file_hash=HASH_B)

    claimed_one = repository.claim_next_job()
    claimed_two = repository.claim_next_job()
    claimed_three = repository.claim_next_job()

    assert claimed_one["id"] == first["id"]
    assert claimed_one["status"] == PROCESSING
    assert claimed_one["started_at"] is not None
    assert claimed_two["id"] == second["id"]
    assert claimed_three is None


def test_stale_processing_jobs_go_back_to_pending(session_factory):
    document, _ = add()
    repository.claim_next_job()

    assert repository.requeue_stale(max_age_seconds=3600) == 0  # still fresh

    # make it old by moving started_at into the past
    from database import SessionLocal
    from models import Document

    with SessionLocal() as db:
        row = db.get(Document, document["id"])
        row.started_at = row.started_at - timedelta(hours=2)
        db.commit()

    assert repository.requeue_stale(max_age_seconds=3600) == 1
    assert repository.get_document_by_id(document["id"])["status"] == PENDING


def test_set_stage_and_set_ready_follow_the_job(session_factory):
    document, _ = add()
    repository.claim_next_job()

    assert repository.set_stage(document["id"], "parsing") is True
    assert repository.get_document_by_id(document["id"])["stage"] == "parsing"

    assert repository.set_ready(document["id"], 7) is True
    done = repository.get_document_by_id(document["id"])
    assert done["status"] == READY
    assert done["chunk_count"] == 7
    assert done["stage"] is None


def test_a_delete_during_a_job_is_not_overwritten(session_factory):
    document, _ = add()
    repository.claim_next_job()
    repository.mark_deleting("user-1", document["id"])

    assert repository.set_stage(document["id"], "embedding") is False
    assert repository.set_ready(document["id"], 5) is False
    assert repository.set_failed(document["id"], "timeout") is False
    assert repository.get_document_by_id(document["id"])["status"] == DELETING


def test_set_failed_records_a_safe_code(session_factory):
    document, _ = add()
    repository.claim_next_job()

    assert repository.set_failed(document["id"], "no_text") is True

    failed = repository.get_document_by_id(document["id"])
    assert failed["status"] == FAILED
    assert failed["error"] == "no_text"


def test_mark_deleting_refuses_someone_elses_document(session_factory):
    document, _ = add(user="user-1")

    assert repository.mark_deleting("user-2", document["id"]) is False
    assert repository.mark_deleting("user-1", document["id"]) is True


def test_delete_row_removes_it(session_factory):
    document, _ = add()

    repository.delete_row(document["id"])

    assert repository.get_document_by_id(document["id"]) is None
    repository.delete_row(document["id"])  # deleting twice is fine
