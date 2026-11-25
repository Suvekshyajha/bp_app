#!/usr/bin/env python3
"""Day2 chat app, fixes (version 2).

  backend/retriever/doc_retrieval.py
    - A chosen selection with FEWER THAN 5 chunks in total is given to the
      model whole, in reading order.
    - When the user chose documents, there is NO similarity check at all.
      The hybrid ranking still orders the chunks and the result is still cut
      to `limit`. The similarity threshold only applies when nothing is chosen
      (whole-library search), so unrelated questions still find nothing there.

  frontend/src/pages/ChatPage.tsx
    - The documents popover closes when a message is sent.

Usage, from the project root (the folder with backend/ and frontend/):

    python apply_fixes_v2.py --dry-run
    python apply_fixes_v2.py

Safe to run on a fresh project OR after apply_fixes.py (version 1): if a
.bak-fix backup exists, the original file is restored from it first, then the
version 2 patches are applied. A .bak-fix copy is kept for the next run.
"""

import argparse
import shutil
import sys
from pathlib import Path

RETRIEVAL = Path("backend/retriever/doc_retrieval.py")
CHATPAGE = Path("frontend/src/pages/ChatPage.tsx")

RETRIEVAL_PATCHES = [
    (
        "constant: a selection under 5 chunks counts as small",
        """# Search this many times `limit` candidates, so dropping weak ones still
# leaves enough good ones.
CANDIDATE_FACTOR = 3
""",
        """# Search this many times `limit` candidates, so dropping weak ones still
# leaves enough good ones.
CANDIDATE_FACTOR = 3

# When the user chose the documents and together they have at most this many
# chunks (4 = "fewer than 5"), the model gets ALL of them in reading order
# instead of a search result. A tiny document has nothing to rank.
SMALL_SELECTION_CHUNKS = int(os.getenv("RETRIEVAL_SMALL_SELECTION_CHUNKS", "4"))
""",
    ),
    (
        "helper: read a whole small selection",
        """def retrieve_document_chunks(
""",
        '''def _whole_selection(
    user_id: str,
    document_ids: list[str],
    dense_vector: list[float],
) -> list[dict] | None:
    """Every chunk of the chosen documents, or None when there are too many.

    Same filter as every other read here (user id + document ids + ready), so
    nothing of another user can come back. Chunks are returned in reading
    order: by document, then by chunk number.
    """

    query_filter = build_filter(user_id, document_ids, ready_only=True)

    if _count(query_filter) > SMALL_SELECTION_CHUNKS:
        return None

    points, _offset = client.scroll(
        collection_name=CHUNKS_COLLECTION,
        scroll_filter=query_filter,
        limit=SMALL_SELECTION_CHUNKS,
        with_payload=True,
        with_vectors=["dense"],  # needed to measure the similarity
    )

    points = sorted(
        points,
        key=lambda p: (
            (p.payload or {}).get("document_id", ""),
            (p.payload or {}).get("chunk_index", 0),
        ),
    )

    items = []

    for point in points:
        stored = _dense_vector_of(point)
        similarity = _dot(dense_vector, stored) if stored else 0.0
        items.append(_result_item(point, similarity))

    return items


def retrieve_document_chunks(
''',
    ),
    (
        "shortcut: a small selection is returned whole",
        """    dense_vector = query_vectors["dense"]
""",
        """    dense_vector = query_vectors["dense"]

    # The user chose the documents and they are small: give the model all of
    # it. A failure here is not fatal, the normal search below still runs.
    if document_ids and min_similarity is None:
        try:
            whole = _whole_selection(user_id, list(document_ids), dense_vector)
        except Exception:
            logger.exception("Could not read the whole selection; searching instead")
            whole = None

        if whole:
            logger.info(
                "Document retrieval (chosen documents): small selection, "
                "returned all %d chunk(s)",
                len(whole),
            )
            return whole
""",
    ),
    (
        "no similarity check for chosen documents (flag)",
        """    threshold = effective_threshold(document_ids, min_similarity)
""",
        """    threshold = effective_threshold(document_ids, min_similarity)

    # The user chose the documents, so the scope is already known: no chunk is
    # refused for a low similarity. Only a whole-library search (nothing
    # chosen), or a caller that passes min_similarity itself, is checked.
    check_similarity = search_all or min_similarity is not None
""",
    ),
    (
        "no similarity check for chosen documents (loop)",
        """        if similarity < threshold:
            dropped.append(similarity)
            continue
""",
        """        if check_similarity and similarity < threshold:
            dropped.append(similarity)
            continue
""",
    ),
]

CHATPAGE_PATCHES = [
    (
        "close the documents popover when a message is sent",
        """      touchConversation(conversationId, query);
      setDraft("");

      void sendExistingMessage(query, messageId, conversationId);
""",
        """      touchConversation(conversationId, query);
      setDraft("");
      // The documents popover has done its job once the question is sent
      setDocsOpen(false);

      void sendExistingMessage(query, messageId, conversationId);
""",
    ),
]


def patch_file(path: Path, patches, dry_run: bool) -> bool:
    if not path.exists():
        print(f"  ! {path} not found. Run this from the project root.")
        return False

    backup = path.with_name(path.name + ".bak-fix")

    # Start from the original if an earlier version of this script ran
    source = backup if backup.exists() else path
    if source is backup:
        print("  (starting from the .bak-fix original)")

    raw = source.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")
    ok = True

    for name, old, new in patches:
        if text.count(old) != 1:
            print(f"  ! cannot apply: {name} (anchor found {text.count(old)} times)")
            ok = False
            continue

        text = text.replace(old, new)
        print(f"  + {name}")

    if ok and not dry_run:
        if not backup.exists():
            shutil.copy2(path, backup)
        out = text.replace("\n", "\r\n") if crlf else text
        path.write_bytes(out.encode("utf-8"))

    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--root", default=".", help="project root (default: .)")
    args = parser.parse_args()

    root = Path(args.root)
    results = []

    for rel, patches in ((RETRIEVAL, RETRIEVAL_PATCHES), (CHATPAGE, CHATPAGE_PATCHES)):
        print(rel)
        results.append(patch_file(root / rel, patches, args.dry_run))

    if not all(results):
        print("\nSome patches did not match; nothing was written for those files.")
        return 1

    print("\nDry run only, nothing written." if args.dry_run else "\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())