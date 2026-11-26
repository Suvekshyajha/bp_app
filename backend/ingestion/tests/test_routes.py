import io
import zipfile

import pytest
from fastapi.testclient import TestClient

import limits
import qdrant_docs
import repository
from chunk_helpers import words
from fakes import FakeQdrant

TOKEN = {"Authorization": "Bearer test-token"}


def headers(user="user-a"):
    return {**TOKEN, "X-User-Id": user}


@pytest.fixture
def store(monkeypatch):
    fake = FakeQdrant()
    monkeypatch.setattr(qdrant_docs, "client", fake)
    return fake


@pytest.fixture
def client(session_factory, store):
    import main

    with TestClient(main.app) as test_client:
        yield test_client


def upload(client, content=b"Hello world, some text.", name="notes.txt", user="user-a"):
    return client.post("/documents", files={"file": (name, content, "application/octet-stream")}, headers=headers(user))


def docx_bytes():
    from docx import Document

    buffer = io.BytesIO()
    document = Document()
    document.add_paragraph("hello")
    document.save(buffer)
    return buffer.getvalue()


# ---------- who is calling ----------

def test_health_needs_no_token(client):
    assert client.get("/health").json() == {"status": "healthy"}


@pytest.mark.parametrize("auth_header", [None, "Bearer wrong", "test-token", "Basic dGVzdA=="])
def test_a_wrong_or_missing_token_gives_401(client, auth_header):
    sent = {"X-User-Id": "user-a"}
    if auth_header:
        sent["Authorization"] = auth_header

    assert client.get("/documents", headers=sent).status_code == 401


def test_the_token_is_checked_before_the_user_header(client):
    assert client.get("/documents", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_a_missing_user_header_gives_400(client):
    assert client.get("/documents", headers=TOKEN).status_code == 400
    assert client.get("/documents", headers={**TOKEN, "X-User-Id": "   "}).status_code == 400
    assert client.get("/documents", headers={**TOKEN, "X-User-Id": "x" * 201}).status_code == 400


def test_the_service_refuses_to_start_without_a_token(session_factory, monkeypatch):
    import main

    monkeypatch.delenv("INGEST_SERVICE_TOKEN")
    monkeypatch.delenv("INGEST_ALLOW_NO_AUTH", raising=False)

    with pytest.raises(RuntimeError, match="INGEST_SERVICE_TOKEN"):
        with TestClient(main.app):
            pass


def test_no_auth_mode_works_on_localhost_only(session_factory, store, monkeypatch):
    import main

    monkeypatch.delenv("INGEST_SERVICE_TOKEN")
    monkeypatch.setenv("INGEST_ALLOW_NO_AUTH", "true")

    with TestClient(main.app) as open_client:
        assert open_client.get("/documents", headers={"X-User-Id": "u"}).status_code == 200

    monkeypatch.setenv("INGEST_BIND_HOST", "0.0.0.0")
    with pytest.raises(RuntimeError, match="INGEST_BIND_HOST"):
        with TestClient(main.app):
            pass


def test_no_docs_pages_are_served(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# ---------- uploading ----------

def test_a_new_upload_is_accepted_and_the_file_waits_for_the_worker(client):
    response = upload(client)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending" and body["filename"] == "notes.txt"
    assert (limits.UPLOAD_DIR / body["id"]).read_bytes() == b"Hello world, some text."
    assert not list(limits.UPLOAD_DIR.glob(".tmp-*"))


def test_the_same_user_and_file_twice_gives_one_document(client):
    first = upload(client)
    second = upload(client)

    assert first.status_code == 202 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert len(client.get("/documents", headers=headers()).json()) == 1


def test_two_users_with_the_same_file_get_two_documents(client):
    one = upload(client, user="user-a").json()["id"]
    two = upload(client, user="user-b").json()["id"]

    assert one != two


def test_a_failed_document_is_queued_again_by_uploading_it_again(client):
    first = upload(client).json()["id"]
    repository.claim_next_job()
    repository.set_failed(first, "timeout")

    again = upload(client)

    assert again.status_code == 202
    assert repository.get_document_by_id(first)["status"] == "pending"
    assert (limits.UPLOAD_DIR / first).exists()


def test_a_file_that_is_too_big_gives_413(client, monkeypatch):
    monkeypatch.setattr(limits, "MAX_UPLOAD_BYTES", 100)

    response = upload(client, content=b"x" * 101)

    assert response.status_code == 413
    assert not list(limits.UPLOAD_DIR.glob(".tmp-*"))


def test_a_file_over_the_limit_is_refused_before_it_is_copied(client, monkeypatch):
    monkeypatch.setattr(limits, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr("routes.MULTIPART_OVERHEAD", 0)

    assert upload(client, content=b"x" * 5000).status_code == 413


def test_a_bad_extension_gives_415(client):
    assert upload(client, name="virus.exe").status_code == 415
    assert upload(client, name="noextension").status_code == 415


def test_wrong_magic_bytes_give_415(client):
    assert upload(client, content=b"MZ this is an exe", name="fake.pdf").status_code == 415
    assert upload(client, content=b"plain text", name="fake.docx").status_code == 415
    assert upload(client, content=b"\xff\xfe\x00bad", name="bad.txt").status_code == 415


def test_an_empty_file_gives_415(client):
    assert upload(client, content=b"", name="empty.txt").status_code == 415


def test_a_real_docx_is_accepted(client):
    assert upload(client, content=docx_bytes(), name="report.docx").status_code == 202


def test_a_zip_without_word_document_xml_gives_415(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("other.txt", "x")

    assert upload(client, content=buffer.getvalue(), name="a.docx").status_code == 415


def test_the_filename_loses_its_folders_and_is_cut_to_120_characters(client):
    clean = upload(client, name="../../etc/passwd.txt").json()["filename"]
    long = upload(client, content=b"another one", name="a" * 300 + ".txt").json()["filename"]

    assert clean == "passwd.txt"
    assert len(long) == 120


def test_the_per_user_cap_gives_409(client, monkeypatch):
    monkeypatch.setattr(limits, "MAX_DOCS_PER_USER", 2)
    assert upload(client, content=b"one").status_code == 202
    assert upload(client, content=b"two").status_code == 202

    third = upload(client, content=b"three")

    assert third.status_code == 409
    assert upload(client, content=b"one").status_code == 200  # an existing one is no new document
    assert upload(client, content=b"three", user="user-b").status_code == 202  # the cap is per user
    assert not list(limits.UPLOAD_DIR.glob(".tmp-*"))


# ---------- reading ----------

def test_user_b_asking_for_user_as_document_looks_exactly_like_a_missing_one(client):
    document_id = upload(client, user="user-a").json()["id"]

    foreign = client.get(f"/documents/{document_id}", headers=headers("user-b"))
    missing = client.get("/documents/does-not-exist", headers=headers("user-b"))

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert client.delete(f"/documents/{document_id}", headers=headers("user-b")).status_code == 404
    assert client.get(f"/documents/{document_id}", headers=headers("user-a")).status_code == 200


def test_the_list_shows_only_the_users_own_documents(client):
    mine = upload(client, content=b"mine", user="user-a").json()["id"]
    upload(client, content=b"theirs", user="user-b")

    listed = client.get("/documents", headers=headers("user-a")).json()

    assert [item["id"] for item in listed] == [mine]
    assert set(listed[0]) == {"id", "filename", "status", "stage", "error", "chunk_count", "created_at", "chunking"}


def test_a_document_shows_its_stage_and_chunking_summary(client):
    document_id = upload(client).json()["id"]
    repository.claim_next_job()
    repository.set_stage(document_id, "embedding")
    repository.save_report(
        document_id,
        '{"used_strategy": "heading_aware", "fallback_reason": "guard:x", "chunk_count": 7, "timings": {}}',
    )

    shown = client.get(f"/documents/{document_id}", headers=headers()).json()

    assert shown["status"] == "processing" and shown["stage"] == "embedding"
    assert shown["chunking"] == {"strategy_used": "heading_aware", "fallback_reason": "guard:x", "chunk_count": 7}
    assert shown["created_at"].endswith("Z")


def test_the_internal_analysis_is_never_returned(client):
    document_id = upload(client).json()["id"]
    repository.save_analysis(document_id, '{"reasoning": "private"}')

    assert "private" not in client.get(f"/documents/{document_id}", headers=headers()).text


# ---------- deleting ----------

def test_delete_removes_the_chunks_then_the_row(client, store):
    document_id = upload(client).json()["id"]
    store.points["p1"] = {"vector": {}, "payload": {"user_id": "user-a", "document_id": document_id}}
    store.points["p2"] = {"vector": {}, "payload": {"user_id": "user-b", "document_id": document_id}}

    response = client.delete(f"/documents/{document_id}", headers=headers())

    assert response.status_code == 204
    assert set(store.points) == {"p2"}
    assert repository.get_document_by_id(document_id) is None
    assert not (limits.UPLOAD_DIR / document_id).exists()


def test_when_qdrant_is_down_delete_gives_503_and_keeps_the_row(client, monkeypatch):
    document_id = upload(client).json()["id"]

    def down(user_id, doc_id):
        raise ConnectionError("qdrant is down")

    monkeypatch.setattr(qdrant_docs, "delete_document", down)

    response = client.delete(f"/documents/{document_id}", headers=headers())

    assert response.status_code == 503
    assert repository.get_document_by_id(document_id)["status"] == "deleting"

    monkeypatch.undo()
    monkeypatch.setattr(qdrant_docs, "client", FakeQdrant())
    assert client.delete(f"/documents/{document_id}", headers=headers()).status_code == 204  # a retry works
