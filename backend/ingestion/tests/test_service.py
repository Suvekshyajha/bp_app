import io
import zipfile

import grpc
import pytest

import ingest_pb2
import limits
import main
import qdrant_docs
import repository
from fakes import FakeQdrant
from grpc_api import OK, Api, metadata

Code = grpc.StatusCode


@pytest.fixture
def store(monkeypatch):
    fake = FakeQdrant()
    monkeypatch.setattr(qdrant_docs, "client", fake)
    return fake


@pytest.fixture
def api(session_factory, store):
    running = Api()
    yield running
    running.close()


def document_id_of(result):
    assert result.code == OK, result.detail
    return result.value.document.id


def docx_bytes():
    from docx import Document

    buffer = io.BytesIO()
    document = Document()
    document.add_paragraph("hello")
    document.save(buffer)
    return buffer.getvalue()


def list_with(api, entries):
    return api.call("ListDocuments", ingest_pb2.ListDocumentsRequest(), entries)


# ---------- who is calling ----------

@pytest.mark.parametrize("auth_header", [None, "Bearer wrong", "test-token", "Basic dGVzdA=="])
def test_a_wrong_or_missing_token_is_unauthenticated(api, auth_header):
    sent = [("x-user-id", "user-a")]
    if auth_header:
        sent.append(("authorization", auth_header))

    assert list_with(api, tuple(sent)).code == Code.UNAUTHENTICATED


def test_the_token_is_checked_before_the_user_id(api):
    assert list_with(api, metadata(user=None, token="wrong")).code == Code.UNAUTHENTICATED


@pytest.mark.parametrize("user", [None, "   ", "x" * 201])
def test_a_missing_or_bad_user_id_is_refused(api, user):
    assert list_with(api, metadata(user=user)).code == Code.PERMISSION_DENIED


def test_an_upload_without_a_token_is_refused(api):
    pieces = iter([ingest_pb2.UploadRequest(info=ingest_pb2.UploadInfo(filename="a.txt"))])

    result = api.call("Upload", pieces, metadata(token=None))

    assert result.code == Code.UNAUTHENTICATED
    assert repository.list_documents("user-a") == []


def test_the_service_refuses_to_start_without_a_token(session_factory, monkeypatch):
    monkeypatch.delenv("INGEST_SERVICE_TOKEN")
    monkeypatch.delenv("INGEST_ALLOW_NO_AUTH", raising=False)

    with pytest.raises(RuntimeError, match="INGEST_SERVICE_TOKEN"):
        main.build_server("127.0.0.1:0")


def test_no_auth_mode_works_on_localhost_only(session_factory, store, monkeypatch):
    monkeypatch.delenv("INGEST_SERVICE_TOKEN")
    monkeypatch.setenv("INGEST_ALLOW_NO_AUTH", "true")

    open_api = Api()
    try:
        assert list_with(open_api, metadata(token=None)).code == OK
    finally:
        open_api.close()

    monkeypatch.setenv("INGEST_BIND_HOST", "0.0.0.0")
    with pytest.raises(RuntimeError, match="INGEST_BIND_HOST"):
        main.build_server("127.0.0.1:0")


# ---------- uploading ----------

def test_a_new_upload_is_queued_and_the_file_waits_for_the_worker(api):
    result = api.upload()

    assert result.code == OK
    answer = result.value
    assert answer.queued and not answer.existing
    assert answer.document.status == "pending" and answer.document.filename == "notes.txt"
    assert (limits.UPLOAD_DIR / answer.document.id).read_bytes() == b"Hello world, some text."
    assert not list(limits.UPLOAD_DIR.glob(".tmp-*"))


def test_a_file_sent_in_many_pieces_arrives_whole(api):
    content = ("line of text\n" * 5000).encode()

    document_id = document_id_of(api.upload(content=content, block=100))

    assert (limits.UPLOAD_DIR / document_id).read_bytes() == content


def test_an_upload_must_start_with_the_file_name(api):
    pieces = iter([ingest_pb2.UploadRequest(chunk=b"text")])

    assert api.call("Upload", pieces, metadata()).code == Code.INVALID_ARGUMENT


def test_the_same_user_and_file_twice_gives_one_document(api):
    first = api.upload()
    second = api.upload()

    assert first.value.queued and second.value.existing and not second.value.queued
    assert first.value.document.id == second.value.document.id
    assert len(api.list().value.documents) == 1


def test_two_users_with_the_same_file_get_two_documents(api):
    one = document_id_of(api.upload(user="user-a"))
    two = document_id_of(api.upload(user="user-b"))

    assert one != two


def test_a_failed_document_is_queued_again_by_uploading_it_again(api):
    first = document_id_of(api.upload())
    repository.claim_next_job()
    repository.set_failed(first, "timeout")

    again = api.upload()

    assert again.value.queued
    assert repository.get_document_by_id(first)["status"] == "pending"
    assert (limits.UPLOAD_DIR / first).exists()


def test_a_file_that_is_too_big_is_out_of_range(api, monkeypatch):
    monkeypatch.setattr(limits, "MAX_UPLOAD_BYTES", 100)

    result = api.upload(content=b"x" * 101, block=10)

    assert result.code == Code.OUT_OF_RANGE
    assert not list(limits.UPLOAD_DIR.glob(".tmp-*"))


@pytest.mark.parametrize("name", ["virus.exe", "noextension"])
def test_a_bad_extension_is_refused(api, name):
    assert api.upload(name=name).code == Code.INVALID_ARGUMENT


@pytest.mark.parametrize("content,name", [
    (b"MZ this is an exe", "fake.pdf"),
    (b"plain text", "fake.docx"),
    (b"\xff\xfe\x00bad", "bad.txt"),
    (b"", "empty.txt"),
])
def test_wrong_or_empty_content_is_refused(api, content, name):
    assert api.upload(content=content, name=name).code == Code.INVALID_ARGUMENT


def test_a_real_docx_is_accepted(api):
    assert api.upload(content=docx_bytes(), name="report.docx").value.queued


def test_a_zip_without_word_document_xml_is_refused(api):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("other.txt", "x")

    assert api.upload(content=buffer.getvalue(), name="a.docx").code == Code.INVALID_ARGUMENT


def test_the_filename_loses_its_folders_and_is_cut_to_120_characters(api):
    clean = api.upload(name="../../etc/passwd.txt").value.document.filename
    long = api.upload(content=b"another one", name="a" * 300 + ".txt").value.document.filename

    assert clean == "passwd.txt"
    assert len(long) == 120


def test_the_per_user_cap_is_a_failed_precondition(api, monkeypatch):
    monkeypatch.setattr(limits, "MAX_DOCS_PER_USER", 2)
    assert api.upload(content=b"one").code == OK
    assert api.upload(content=b"two").code == OK

    third = api.upload(content=b"three")

    assert third.code == Code.FAILED_PRECONDITION
    assert api.upload(content=b"one").value.existing  # an existing one is no new document
    assert api.upload(content=b"three", user="user-b").code == OK  # the cap is per user
    assert not list(limits.UPLOAD_DIR.glob(".tmp-*"))


# ---------- reading ----------

def test_user_b_asking_for_user_as_document_looks_exactly_like_a_missing_one(api):
    document_id = document_id_of(api.upload(user="user-a"))

    foreign = api.get(document_id, "user-b")
    missing = api.get("does-not-exist", "user-b")

    assert foreign.code == missing.code == Code.NOT_FOUND
    assert foreign.detail == missing.detail
    assert api.delete(document_id, "user-b").code == Code.NOT_FOUND
    assert api.get(document_id, "user-a").code == OK


def test_the_list_shows_only_the_users_own_documents(api):
    mine = document_id_of(api.upload(content=b"mine", user="user-a"))
    api.upload(content=b"theirs", user="user-b")

    listed = api.list("user-a").value.documents

    assert [item.id for item in listed] == [mine]


def test_a_document_shows_its_stage_and_chunking_summary(api):
    document_id = document_id_of(api.upload())
    repository.claim_next_job()
    repository.set_stage(document_id, "embedding")
    repository.save_report(
        document_id,
        '{"used_strategy": "heading_aware", "fallback_reason": "guard:x", "chunk_count": 7, "timings": {}}',
    )

    shown = api.get(document_id).value

    assert shown.status == "processing" and shown.stage == "embedding"
    assert not shown.HasField("error")
    assert shown.chunking.strategy_used == "heading_aware"
    assert shown.chunking.fallback_reason == "guard:x"
    assert shown.chunking.chunk_count == 7
    assert shown.created_at.endswith("Z")


def test_the_internal_analysis_is_never_returned(api):
    document_id = document_id_of(api.upload())
    repository.save_analysis(document_id, '{"reasoning": "private"}')

    assert "private" not in str(api.get(document_id).value)


# ---------- deleting ----------

def test_delete_removes_the_chunks_then_the_row(api, store):
    document_id = document_id_of(api.upload())
    store.points["p1"] = {"vector": {}, "payload": {"user_id": "user-a", "document_id": document_id}}
    store.points["p2"] = {"vector": {}, "payload": {"user_id": "user-b", "document_id": document_id}}

    assert api.delete(document_id).code == OK

    assert set(store.points) == {"p2"}
    assert repository.get_document_by_id(document_id) is None
    assert not (limits.UPLOAD_DIR / document_id).exists()


def test_when_qdrant_is_down_delete_is_unavailable_and_keeps_the_row(api, monkeypatch):
    document_id = document_id_of(api.upload())

    def down(user_id, doc_id):
        raise ConnectionError("qdrant is down")

    monkeypatch.setattr(qdrant_docs, "delete_document", down)

    assert api.delete(document_id).code == Code.UNAVAILABLE
    assert repository.get_document_by_id(document_id)["status"] == "deleting"

    monkeypatch.undo()
    monkeypatch.setattr(qdrant_docs, "client", FakeQdrant())
    assert api.delete(document_id).code == OK  # a retry works
