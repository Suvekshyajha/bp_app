"""Step 2 of the migration: finish backend/shared_common.

Run from the repository root:   python step2_apply.py
Run it once. Each edit is checked before it is made, and the script stops with
a message ("STOP: ...") if a file does not look as expected. Running it a
second time stops at the first edit, because that edit is already in place.
"""

import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
COMMON = ROOT / "backend" / "shared_common"
PKG = COMMON / "common"
RETRIEVER = ROOT / "backend" / "retriever"


def die(message):
    print(f"STOP: {message}")
    sys.exit(1)


def read(path):
    raw = path.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    return raw.replace("\r\n", "\n"), crlf


def write(path, text, crlf=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if crlf:
        text = text.replace("\n", "\r\n")
    path.write_bytes(text.encode("utf-8"))
    print(f"  wrote   {path.relative_to(ROOT)}")


# ---------------------------------------------------------------- new files

SANITIZE = '''"""Cleaning of untrusted text before it goes into a prompt.

Used by the retriever (chat messages and document excerpts) and by the
ingestion service (document text). Two things are removed:

1. Characters a person cannot see but a model can read. Attackers hide
   instructions in them ("ASCII smuggling"): Unicode tag characters, zero
   width space / word joiner / BOM, bidi overrides and control characters.
   NOT removed: U+200C and U+200D (zero-width non-joiner / joiner), which
   Nepali, Hindi, Persian and emoji sequences need to display correctly.

2. Our own delimiter tags, in any spelling, so stored text cannot close a
   <past_messages> or <document_excerpts> block and write outside it.
"""

import re


HIDDEN_CHARACTERS = re.compile(
    "["
    "\\u200b"                # zero-width space
    "\\u2060"                # word joiner
    "\\ufeff"                # byte order mark
    "\\u202a-\\u202e"         # bidi embeddings and overrides
    "\\u2066-\\u2069"         # bidi isolates
    "\\U000e0000-\\U000e007f"  # Unicode tag characters
    "\\U000e0100-\\U000e01ef"  # variation selectors supplement
    "\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f-\\x9f"  # control characters (not \\t \\n \\r)
    "]"
)

DELIMITER_TAG = re.compile(
    r"<\\s*/?\\s*(?:past_messages|document_excerpts)\\b[^>]*>?",
    re.IGNORECASE,
)


def strip_hidden_characters(text: str) -> str:
    """Remove invisible characters that can carry hidden instructions."""

    return HIDDEN_CHARACTERS.sub("", text)


def clean_for_prompt(text: str) -> str:
    """Text read from storage, made safe to place inside a prompt block.

    The cleaning is repeated until the text stops changing. One pass is not
    enough: removing "</past_messages>" from
    "</past_mess</past_messages>ages>" leaves a new working tag behind.
    Every pass only removes characters, so the loop always ends.
    """

    previous = None

    while previous != text:
        previous = text
        text = DELIMITER_TAG.sub("", strip_hidden_characters(text))

    return text
'''

SETTINGS = '''"""Settings both services must agree on, so the models cannot drift apart."""

import uuid

DENSE_MODEL = "BAAI/bge-base-en-v1.5"
SPARSE_MODEL = "Qdrant/bm25"
DENSE_DIM = 768

MESSAGES_COLLECTION = "chat_messages"
CHUNKS_COLLECTION = "document_chunks"

# Used to build document and chunk ids (uuid5). NEVER change it: every stored
# id would change and old documents could no longer be found or replaced.
DOC_NAMESPACE = uuid.UUID("2bb65330-cdce-40b7-a780-cd1fa3700fbd")
'''

EMBEDDINGS = '''"""Dense and sparse embeddings, shared by the retriever and the ingestion worker.

The models load on first use, not when this file is imported, so importing it
is cheap and never needs a model download. The retriever calls warm_up() at
start so the first chat turn is not slow. Services that must stay light (the
orchestrator and the ingestion API process) must never call anything here.
"""

import threading

from common.settings import DENSE_MODEL, SPARSE_MODEL

__all__ = [
    "get_dense",
    "get_sparse",
    "warm_up",
    "embed_messages",
    "embed_passages",
    "embed_query",
    "count_tokens",
]

_dense = None
_sparse = None
_load_lock = threading.Lock()


def get_dense():
    """The dense model, loaded once on first use."""

    global _dense

    if _dense is None:
        with _load_lock:
            if _dense is None:
                from sentence_transformers import SentenceTransformer

                _dense = SentenceTransformer(DENSE_MODEL)

    return _dense


def get_sparse():
    """The sparse (BM25) model, loaded once on first use."""

    global _sparse

    if _sparse is None:
        with _load_lock:
            if _sparse is None:
                from fastembed import SparseTextEmbedding

                _sparse = SparseTextEmbedding(model_name=SPARSE_MODEL)

    return _sparse


def warm_up() -> None:
    """Load both models now (call at start up)."""

    get_dense()
    get_sparse()


def embed_messages(messages: list[str]) -> list[dict]:
    """Generate dense and sparse embeddings for chat messages."""

    if not messages:
        return []

    dense_vectors = get_dense().encode(
        messages,
        normalize_embeddings=True,
    )

    sparse_vectors = list(get_sparse().embed(messages))

    results = []

    for dense, sparse in zip(dense_vectors, sparse_vectors):
        results.append({
            "dense": dense.tolist(),
            "sparse": {
                "indices": sparse.indices.tolist(),
                "values": sparse.values.tolist(),
            },
        })

    return results


# Document chunks are embedded exactly like messages (no query prefix).
embed_passages = embed_messages


def embed_query(text: str) -> dict:
    """Generate embeddings for a conversational search query."""

    query = (
        "Represent this sentence for searching relevant passages: "
        + text
    )

    dense_vector = get_dense().encode(
        query,
        normalize_embeddings=True,
    )

    sparse_vector = list(get_sparse().query_embed(text))[0]

    return {
        "dense": dense_vector.tolist(),
        "sparse": {
            "indices": sparse_vector.indices.tolist(),
            "values": sparse_vector.values.tolist(),
        },
    }


def count_tokens(text: str) -> int:
    """Tokens the dense model would read (no special tokens added).

    Chunk sizes are measured in these, because bge-base reads at most 512.
    """

    return len(get_dense().tokenizer.encode(text, add_special_tokens=False))
'''

TEST_SANITIZE = '''from common import sanitize


def test_invisible_characters_are_removed():
    hidden = "hi\\u200b there\\u2060!\\ufeff\\U000e0049\\U000e0067\\u202e"

    assert sanitize.strip_hidden_characters(hidden) == "hi there!"


def test_normal_text_is_not_changed():
    text = "line one\\n\\tindented <b>html</b> and code: a < b > c"

    assert sanitize.strip_hidden_characters(text) == text
    assert sanitize.clean_for_prompt(text) == text


def test_joiners_needed_by_real_scripts_are_kept():
    nepali = "क्\\u200dष \\u200c emoji 👨\\u200d👩\\u200d👧"

    assert sanitize.strip_hidden_characters(nepali) == nepali


import pytest


@pytest.mark.parametrize("tag", [
    "</past_messages>",
    "</PAST_MESSAGES>",
    "</ past_messages >",
    "<past_messages>",
    "<Past_Messages foo>",
    "</document_excerpts>",
    "</DOCUMENT_EXCERPTS>",
    "</ document_excerpts >",
    "<document_excerpts>",
    "<Document_Excerpts foo>",
])
def test_the_delimiter_tag_is_removed_in_every_spelling(tag):
    assert sanitize.clean_for_prompt(f"before {tag} after") == "before  after"


@pytest.mark.parametrize("sneaky", [
    # Removing the tag once leaves a brand new, working tag behind.
    "a </past_mess</past_messages>ages> b",
    "a </document_exc</document_excerpts>erpts> b",
])
def test_a_split_up_delimiter_tag_cannot_re_form(sneaky):
    cleaned = sanitize.clean_for_prompt(sneaky).lower()

    assert "past_messages" not in cleaned
    assert "document_excerpts" not in cleaned
'''

TEST_LAZY = '''import subprocess
import sys


def test_importing_embeddings_does_not_load_any_model():
    """The orchestrator and the ingestion API must be able to import
    common.embeddings without pulling in torch or downloading a model."""

    code = (
        "import sys, common.embeddings;"
        "bad = [m for m in ('torch', 'sentence_transformers', 'fastembed')"
        " if m in sys.modules];"
        "sys.exit(1 if bad else 0)"
    )

    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
'''

print("New files:")
write(PKG / "sanitize.py", SANITIZE)
write(PKG / "settings.py", SETTINGS)
write(PKG / "embeddings.py", EMBEDDINGS)
write(COMMON / "tests" / "test_sanitize.py", TEST_SANITIZE)
write(COMMON / "tests" / "test_embeddings_lazy.py", TEST_LAZY)
if not (PKG / "__init__.py").exists():
    write(PKG / "__init__.py", "")

# ------------------------------------------------------------ edit existing

print("Edits:")

# retrieval.py: the sanitizer now lives in common.sanitize (names re-exported)
path = RETRIEVER / "retrieval.py"
text, crlf = read(path)

block = re.compile(
    r"# ---------- Prompt-injection hygiene ----------.*?"
    r"def clean_for_prompt.*?\n    return text\n",
    re.S,
)
if len(block.findall(text)) != 1:
    die("retrieval.py: could not find the prompt-injection block exactly once")
if "from common.sanitize import" in text:
    die("retrieval.py already uses common.sanitize (script already applied?)")

text = block.sub(
    "# The prompt-injection cleaning lives in common/sanitize.py now. The names\n"
    "# are imported above, so `retrieval.clean_for_prompt` etc. keep working.\n",
    text,
)
text = text.replace("import re\n", "", 1)
text = text.replace(
    "from crypto import decrypt\n",
    "from common.sanitize import (  # noqa: F401  (re-exported for main.py and tests)\n"
    "    DELIMITER_TAG,\n"
    "    HIDDEN_CHARACTERS,\n"
    "    clean_for_prompt,\n"
    "    strip_hidden_characters,\n"
    ")\n"
    "from crypto import decrypt\n",
    1,
)
write(path, text, crlf)

# tests/test_retrieval.py: the 5 pure sanitizer tests moved to common
path = RETRIEVER / "tests" / "test_retrieval.py"
text, crlf = read(path)
moved = re.compile(
    r"def test_invisible_characters_are_removed\(\):.*?"
    r"(?=def test_retrieved_messages_are_cleaned_before_they_reach_the_model)",
    re.S,
)
if len(moved.findall(text)) != 1:
    die("tests/test_retrieval.py: could not find the 5 sanitizer tests")
text = moved.sub("", text)
write(path, text, crlf)

# main.py: warm up the models at start
path = RETRIEVER / "main.py"
text, crlf = read(path)
a = "from database import Base, engine, migrate_schema\n"
b = "    await asyncio.to_thread(create_collection)\n"
if text.count(a) != 1 or text.count(b) != 1:
    die("main.py: could not find the import line or create_collection call")
text = text.replace(a, a + "from embeddings import warm_up\n", 1)
text = text.replace(
    b,
    b + "\n    # Load the embedding models now, so the first chat turn is not slow\n"
        "    await asyncio.to_thread(warm_up)\n",
    1,
)
write(path, text, crlf)


# service_auth: a `name` argument, so ingestion's errors say INGEST_SERVICE_TOKEN
path = PKG / "service_auth.py"
if not path.exists():
    die("shared_common/common/service_auth.py not found (was it moved in step 2?)")
text, crlf = read(path)
if "name: str" in text:
    die("service_auth.py already has a `name` argument (script already applied?)")

SWAPS = [
    ("    bind_host: str,\n) -> str | None:",
     '    bind_host: str,\n    name: str = "CHAT_SERVICE_TOKEN",\n) -> str | None:'),
    ('    """Return the token to enforce, or None if running without one is\n'
     '    explicitly allowed. Raises RuntimeError if the service must not start."""\n',
     '    """Return the token to enforce, or None if running without one is\n'
     '    explicitly allowed. Raises RuntimeError if the service must not start.\n\n'
     '    `name` is the environment variable the token comes from. It only changes\n'
     '    the wording of the errors: "INGEST_SERVICE_TOKEN" also gives\n'
     '    INGEST_ALLOW_NO_AUTH and INGEST_BIND_HOST in the text.\n'
     '    """\n\n'
     '    prefix = name.removesuffix("_SERVICE_TOKEN")\n'),
    ('"CHAT_SERVICE_TOKEN is still the example value. " + HOW_TO_MAKE_ONE',
     'f"{name} is still the example value. " + HOW_TO_MAKE_ONE'),
    ('"CHAT_SERVICE_TOKEN is short (%d characters). Use at least %d.",\n                len(token),',
     '"%s is short (%d characters). Use at least %d.",\n                name,\n                len(token),'),
    ('"CHAT_SERVICE_TOKEN is not set, so the service will not start: "',
     'f"{name} is not set, so the service will not start: "'),
    ('"delete any user\'s chats. " + HOW_TO_MAKE_ONE + " "',
     '"delete any user\'s data. " + HOW_TO_MAKE_ONE + " "'),
    ('"(For local development only: CHAT_ALLOW_NO_AUTH=true.)"',
     'f"(For local development only: {prefix}_ALLOW_NO_AUTH=true.)"'),
    ('"CHAT_ALLOW_NO_AUTH=true is only allowed when CHAT_BIND_HOST is "\n'
     '            "this machine (127.0.0.1). Set CHAT_SERVICE_TOKEN instead."',
     'f"{prefix}_ALLOW_NO_AUTH=true is only allowed when {prefix}_BIND_HOST is "\n'
     '            f"this machine (127.0.0.1). Set {name} instead."'),
    ('"Running WITHOUT a service token (CHAT_ALLOW_NO_AUTH=true). "',
     'f"Running WITHOUT a service token ({prefix}_ALLOW_NO_AUTH=true). "'),
    ('"Development only: any program on this machine can read every chat."',
     '"Development only: any program on this machine can read all data."'),
]
for old, new in SWAPS:
    if text.count(old) != 1:
        die(f"service_auth.py: expected this text exactly once:\n{old}")
    text = text.replace(old, new, 1)
write(path, text, crlf)

# a test for it, appended to the moved test file
path = COMMON / "tests" / "test_service_auth.py"
text, crlf = read(path)
if "INGEST_SERVICE_TOKEN" in text:
    die("test_service_auth.py already has the custom-name test")
text = text.rstrip("\n") + '''


def test_a_custom_name_appears_in_every_error_text():
    ingest = {"name": "INGEST_SERVICE_TOKEN"}

    with pytest.raises(RuntimeError, match="INGEST_SERVICE_TOKEN is not set"):
        require_service_token(None, False, "127.0.0.1", **ingest)

    with pytest.raises(RuntimeError, match="INGEST_ALLOW_NO_AUTH=true"):
        require_service_token(None, False, "127.0.0.1", **ingest)

    with pytest.raises(RuntimeError, match="INGEST_BIND_HOST"):
        require_service_token(None, True, "0.0.0.0", **ingest)

    with pytest.raises(RuntimeError, match="INGEST_SERVICE_TOKEN is still the example"):
        require_service_token("change_me", False, "127.0.0.1", **ingest)


def test_the_default_name_is_unchanged_for_the_chat_service():
    with pytest.raises(RuntimeError, match="CHAT_SERVICE_TOKEN is not set"):
        require_service_token(None, False, "127.0.0.1")
'''
write(path, text, crlf)

print("Done. Now run the tests (see the instructions).")