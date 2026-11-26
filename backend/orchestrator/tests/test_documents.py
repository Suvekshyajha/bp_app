"""The document routes: session required, limits, filename cleaning, rate limit,
error mapping, and the user id always comes from the session."""

import pytest

import documents
from conftest import IngestError, login, make_user
from rate_limit import SlidingWindowRateLimiter

DOC = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def generous_limiter(monkeypatch):
    """Default: a limit so high it never gets in the way."""
    monkeypatch.setattr(documents, "doc_upload_limiter", SlidingWindowRateLimiter(1000, 60))


def upload(client, name="notes.txt", content=b"hello", **form):
    return client.post(
        "/api/documents",
        files={"file": (name, content, "application/octet-stream")},
        data=form,
    )


# ---------- a session is required ----------

@pytest.mark.parametrize("method, path", [
    ("post", "/api/documents"),
    ("get", "/api/documents"),
    ("get", f"/api/documents/{DOC}"),
    ("delete", f"/api/documents/{DOC}"),
])
def test_every_route_needs_a_session(client, fake_ingest, method, path):
    kwargs = {"files": {"file": ("a.txt", b"x", "text/plain")}} if method == "post" else {}

    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401
    assert fake_ingest.calls == []


# ---------- upload ----------

def test_upload_is_forwarded_for_the_session_user(logged_in, user, fake_ingest):
    response = upload(logged_in)

    assert response.status_code == 202
    assert response.json() == {"id": DOC, "filename": "notes.txt", "status": "pending"}
    assert fake_ingest.uploads == [
        {"user_id": user.id, "filename": "notes.txt", "content": b"hello"}
    ]


def test_an_existing_document_is_passed_on_as_200(logged_in, fake_ingest):
    fake_ingest.upload_result = (200, {"id": DOC, "filename": "notes.txt", "status": "ready"})

    response = upload(logged_in)

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_the_user_id_comes_from_the_session_even_if_the_form_sends_another(logged_in, user, fake_ingest):
    response = upload(logged_in, user_id="mallory", x_user_id="mallory")

    assert response.status_code == 202
    assert [u["user_id"] for u in fake_ingest.uploads] == [user.id]


def test_the_filename_loses_its_folders(logged_in, fake_ingest):
    upload(logged_in, name="..\\..\\secret/dir/report.txt")

    assert fake_ingest.uploads[0]["filename"] == "report.txt"


def test_clean_filename_drops_folders_and_control_characters():
    assert documents.clean_filename("..\\..\\secret/dir/re\x01port\x7f.txt") == "report.txt"
    assert documents.clean_filename("  spaced.md  ") == "spaced.md"
    assert documents.clean_filename(None) == ""


def test_a_long_filename_is_cut_to_120_characters(logged_in, fake_ingest):
    upload(logged_in, name="a" * 300 + ".txt")

    assert len(fake_ingest.uploads[0]["filename"]) == documents.MAX_FILENAME_CHARS


@pytest.mark.parametrize("name", ["virus.exe", "noextension", "photo.png", "archive.pdf.exe", ".txt.bak"])
def test_other_file_types_are_refused_and_never_forwarded(logged_in, fake_ingest, name):
    response = upload(logged_in, name=name)

    assert response.status_code == 415
    assert fake_ingest.calls == []


@pytest.mark.parametrize("name", ["a.pdf", "a.DOCX", "a.txt", "a.md"])
def test_the_four_allowed_types_are_accepted(logged_in, name):
    assert upload(logged_in, name=name).status_code == 202


def test_an_empty_file_is_refused(logged_in, fake_ingest):
    response = upload(logged_in, content=b"")

    assert response.status_code == 415
    assert fake_ingest.calls == []


def test_a_body_that_is_too_big_is_refused_from_its_content_length(logged_in, fake_ingest, monkeypatch):
    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(documents, "MULTIPART_OVERHEAD", 0)

    response = upload(logged_in, content=b"x" * 5000)

    assert response.status_code == 413
    assert fake_ingest.calls == []


def test_a_file_that_is_too_big_is_refused_while_counting_bytes(logged_in, fake_ingest, monkeypatch):
    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(documents, "MULTIPART_OVERHEAD", 100_000)  # the early check passes

    response = upload(logged_in, content=b"x" * 11)

    assert response.status_code == 413
    assert fake_ingest.calls == []


def test_a_file_exactly_at_the_limit_is_accepted(logged_in, monkeypatch):
    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(documents, "MULTIPART_OVERHEAD", 100_000)

    assert upload(logged_in, content=b"x" * 10).status_code == 202


def test_a_request_without_a_file_is_a_bad_request(logged_in, fake_ingest):
    response = logged_in.post("/api/documents", data={"note": "no file here"})

    assert response.status_code == 400
    assert fake_ingest.calls == []


def test_two_files_in_one_request_are_refused(logged_in, fake_ingest):
    response = logged_in.post(
        "/api/documents",
        files=[
            ("file", ("a.txt", b"one", "text/plain")),
            ("file", ("b.txt", b"two", "text/plain")),
        ],
    )

    assert response.status_code == 400
    assert fake_ingest.calls == []


# ---------- rate limit ----------

def test_uploads_over_the_limit_get_429_with_retry_after(logged_in, fake_ingest, monkeypatch):
    monkeypatch.setattr(documents, "doc_upload_limiter", SlidingWindowRateLimiter(1, 60))

    assert upload(logged_in).status_code == 202
    blocked = upload(logged_in)

    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert len(fake_ingest.uploads) == 1  # the blocked one never reached ingestion


def test_the_upload_limit_is_per_user(client, db, fake_ingest, monkeypatch):
    monkeypatch.setattr(documents, "doc_upload_limiter", SlidingWindowRateLimiter(1, 60))
    alice, bob = make_user(db, "alice"), make_user(db, "bob")

    login(client, db, alice)
    assert upload(client).status_code == 202
    assert upload(client).status_code == 429

    login(client, db, bob)
    assert upload(client).status_code == 202


# ---------- error mapping ----------

@pytest.mark.parametrize("ingest_status, expected", [
    (404, 404),
    (409, 409),
    (413, 413),
    (415, 415),
    (503, 503),
    (401, 502),  # a wrong service token is our fault: never a logout for the browser
    (400, 502),
    (500, 502),
])
def test_ingestion_errors_are_mapped(logged_in, fake_ingest, ingest_status, expected):
    fake_ingest.error = IngestError(ingest_status, "from ingestion")

    assert upload(logged_in).status_code == expected
    assert logged_in.get("/api/documents").status_code == expected
    assert logged_in.get(f"/api/documents/{DOC}").status_code == expected
    assert logged_in.delete(f"/api/documents/{DOC}").status_code == expected


def test_an_ingestion_message_for_the_user_is_passed_on(logged_in, fake_ingest):
    fake_ingest.error = IngestError(409, "You can keep at most 20 documents. Delete one first.")

    response = upload(logged_in)

    assert response.json()["detail"] == "You can keep at most 20 documents. Delete one first."


def test_an_unreachable_service_does_not_leak_details(logged_in, fake_ingest):
    fake_ingest.error = IngestError(503, "connect ECONNREFUSED 10.0.0.7:8100")

    response = logged_in.get("/api/documents")

    assert response.status_code == 503
    assert "10.0.0.7" not in response.text


# ---------- list, get, delete ----------

def test_list_is_forwarded_for_the_session_user(logged_in, user, fake_ingest):
    fake_ingest.documents = [{"id": DOC, "filename": "a.txt", "status": "ready"}]

    response = logged_in.get("/api/documents")

    assert response.json() == fake_ingest.documents
    assert fake_ingest.calls == [("list", user.id)]


def test_get_is_forwarded_for_the_session_user(logged_in, user, fake_ingest):
    response = logged_in.get(f"/api/documents/{DOC}")

    assert response.status_code == 200
    assert fake_ingest.calls == [("get", user.id, DOC)]


def test_delete_is_forwarded_and_gives_204(logged_in, user, fake_ingest):
    response = logged_in.delete(f"/api/documents/{DOC}")

    assert response.status_code == 204
    assert fake_ingest.calls == [("delete", user.id, DOC)]


@pytest.mark.parametrize("bad", ["not-a-uuid", "123", "..%2F..%2Fadmin", "11111111-1111-4111-8111-11111111111g"])
def test_an_id_that_is_not_a_uuid_is_not_found_and_never_forwarded(logged_in, fake_ingest, bad):
    assert logged_in.get(f"/api/documents/{bad}").status_code == 404
    assert logged_in.delete(f"/api/documents/{bad}").status_code == 404
    assert fake_ingest.calls == []


def test_an_uppercase_uuid_is_sent_in_its_canonical_form(logged_in, user, fake_ingest):
    logged_in.get(f"/api/documents/{DOC.upper()}")

    assert fake_ingest.calls == [("get", user.id, DOC)]
