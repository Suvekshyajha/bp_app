"""Search the chunks of the user's documents (read only).

The ingestion service writes the `document_chunks` collection. This file only
reads it. Every filter here holds the user id, so an id that belongs to someone
else behaves exactly like an id that does not exist.

This file does not import retrieval.py on purpose: it stays small and has no
other dependency than Qdrant, the encryption helpers and the cleaning helpers.
"""

import logging
import os

from qdrant_client import models

from common.sanitize import clean_for_prompt
from common.settings import CHUNKS_COLLECTION
from crypto import decrypt
from qdrant_store import client

logger = logging.getLogger(__name__)

# Same meaning as in retrieval.py: the dense cosine similarity a chunk must
# reach to be returned. Tune it from the "Document retrieval:" log line.
MIN_SIMILARITY = float(os.getenv("RETRIEVAL_MIN_SIMILARITY", "0.79"))

# The threshold used when the user CHOSE the documents (document_ids is a
# list). The scope is already known then, so a chunk is not refused only
# because its dense similarity is below the global threshold above: a
# question worded very differently from the text, or a keyword match with a
# weak dense score, still reaches the model. It is NOT "return everything":
# the chunks are still ranked by the hybrid search and cut to `limit`, and
# only chunks that point away from the question (similarity below this
# floor) are dropped. The effective value is never higher than
# MIN_SIMILARITY. Set it to MIN_SIMILARITY (or higher) to switch this off.
SELECTED_MIN_SIMILARITY = float(os.getenv("RETRIEVAL_SELECTED_MIN_SIMILARITY", "0.65"))

# Search this many times `limit` candidates, so dropping weak ones still
# leaves enough good ones.
CANDIDATE_FACTOR = 3

# When the user chose the documents and together they have at most this many
# chunks (4 = "fewer than 5"), the model gets ALL of them in reading order
# instead of a search result. A tiny document has nothing to rank.
SMALL_SELECTION_CHUNKS = int(os.getenv("RETRIEVAL_SMALL_SELECTION_CHUNKS", "4"))

# When the whole library is searched the candidates come from many documents
# and one document can fill most of them, so look at more of them.
ALL_DOCUMENTS_CANDIDATE_FACTOR = 5

# When more than one document is searched, one document may take at most this
# many of the `limit` places on the first pass, so a question that several
# documents touch is not answered from only one of them. Places that no other
# document can fill are given back, so a question that only ONE document can
# answer still gets its full share.
MAX_PER_DOCUMENT = 4

# The best excerpts also bring the chunks right before and after them, because
# an answer often runs over a chunk border. These come after the normal
# excerpts, so when the prompt budget is short they are the first to go.
NEIGHBOR_HITS = 2
NEIGHBOR_RADIUS = 1

# The most documents listed by list_documents.
MAX_LISTED_DOCUMENTS = 50

READY = "ready"
PROCESSING = "processing"
MISSING = "missing"


def build_filter(
    user_id: str,
    document_ids: list[str] | None = None,
    ready_only: bool = True,
) -> models.Filter:
    """A filter that always holds the user id.

    With `document_ids` only those documents match. With `ready_only` only
    chunks of finished documents match (the ingestion worker marks a
    document's chunks "ready" in one call, after the last one is stored).
    """

    # Without a user id the filter would be too wide: refuse instead of leaking
    if not user_id:
        raise ValueError("user_id is required")

    must = [
        models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id)),
    ]

    if document_ids is not None:
        must.append(
            models.FieldCondition(
                key="document_id",
                match=models.MatchAny(any=list(document_ids)),
            )
        )

    if ready_only:
        must.append(
            models.FieldCondition(key="status", match=models.MatchValue(value=READY))
        )

    return models.Filter(must=must)


def _collection_exists() -> bool:
    return client.collection_exists(CHUNKS_COLLECTION)


def _count(query_filter: models.Filter) -> int:
    return client.count(
        collection_name=CHUNKS_COLLECTION,
        count_filter=query_filter,
        exact=True,
    ).count


def document_states(user_id: str, document_ids: list[str]) -> dict[str, str]:
    """For each id: "ready", "processing" or "missing".

    "missing" also covers the documents of other users (the user id is always
    in the filter) and documents whose chunks are not stored yet.
    """

    if not user_id:
        raise ValueError("user_id is required")

    # Nothing has been ingested yet: no document exists
    if not _collection_exists():
        return {document_id: MISSING for document_id in document_ids}

    states: dict[str, str] = {}

    for document_id in document_ids:
        if _count(build_filter(user_id, [document_id], ready_only=True)) > 0:
            states[document_id] = READY
        elif _count(build_filter(user_id, [document_id], ready_only=False)) > 0:
            states[document_id] = PROCESSING
        else:
            states[document_id] = MISSING

    return states


def effective_threshold(
    document_ids: list[str] | None,
    min_similarity: float | None = None,
) -> float:
    """The dense similarity a chunk must reach, for this kind of search.

      - an explicit `min_similarity` always wins (callers and tests);
      - documents the user chose: the relaxed SELECTED_MIN_SIMILARITY, but
        never stricter than the global one;
      - no documents chosen (whole library): the global MIN_SIMILARITY, so a
        question no document covers still finds nothing.
    """

    if min_similarity is not None:
        return min_similarity

    if document_ids:  # a non-empty list: the user chose these documents
        return min(MIN_SIMILARITY, SELECTED_MIN_SIMILARITY)

    return MIN_SIMILARITY


def _dot(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two unit-length vectors (both are normalized)."""

    return sum(x * y for x, y in zip(a, b))


def _dense_vector_of(point) -> list[float] | None:
    vector = point.vector

    if isinstance(vector, dict):  # named vectors come back as {"dense": [...]}
        vector = vector.get("dense")

    return vector or None


def _plain(value) -> str:
    """Decrypt a stored text field and make it safe to place in a prompt."""

    if not value:
        return ""

    return clean_for_prompt(decrypt(value))


def _result_item(point, similarity: float) -> dict:
    """One search result, decrypted and cleaned, in the shape of retrieve_messages."""

    payload = point.payload or {}

    return {
        "id": str(point.id),
        "score": similarity,
        "payload": {
            "document_id": payload.get("document_id", ""),
            "filename": _plain(payload.get("filename")),
            "chunk_index": payload.get("chunk_index", 0),
            "section_title": _plain(payload.get("section_title")),
            "page": payload.get("page"),
            "content": _plain(payload.get("content")),
            "timestamp": payload.get("created_at"),
        },
    }


def _select(ranked: list, limit: int, per_document: int) -> list:
    """The best `limit` entries of `ranked` (best first), spread over documents.

    No document gets more than `per_document` places on the first pass. If
    places are left over (the other documents had nothing relevant), the best
    entries that were held back fill them. The result keeps the ranking order.
    """

    chosen: list[int] = []
    held_back: list[int] = []
    taken: dict[str, int] = {}

    for position, (point, _similarity) in enumerate(ranked):
        document_id = (point.payload or {}).get("document_id", "")

        if len(chosen) < limit and taken.get(document_id, 0) < per_document:
            chosen.append(position)
            taken[document_id] = taken.get(document_id, 0) + 1
        else:
            held_back.append(position)

    for position in held_back:
        if len(chosen) >= limit:
            break

        chosen.append(position)

    chosen.sort()

    return [ranked[position] for position in chosen]


def _neighbours(user_id: str, hits: list, taken: set) -> list:
    """The chunks next to the best hits, as Qdrant points.

    `taken` holds (document_id, chunk_index) of every chunk already chosen; it
    is updated here, so a chunk is never returned twice. The filter holds the
    user id and the document id, like every other search in this file.
    """

    found = []

    for point, _similarity in hits[:NEIGHBOR_HITS]:
        payload = point.payload or {}
        document_id = payload.get("document_id")
        index = payload.get("chunk_index")

        if not document_id or not isinstance(index, int):
            continue

        wanted = [
            number
            for number in range(index - NEIGHBOR_RADIUS, index + NEIGHBOR_RADIUS + 1)
            if number >= 0 and (document_id, number) not in taken
        ]

        if not wanted:
            continue

        query_filter = build_filter(user_id, [document_id], ready_only=True)
        query_filter.must.append(
            models.FieldCondition(
                key="chunk_index",
                match=models.MatchAny(any=wanted),
            )
        )

        points, _offset = client.scroll(
            collection_name=CHUNKS_COLLECTION,
            scroll_filter=query_filter,
            limit=len(wanted),
            with_payload=True,
            with_vectors=["dense"],  # needed to measure the similarity
        )

        for neighbour in sorted(
            points, key=lambda p: (p.payload or {}).get("chunk_index", 0)
        ):
            key = (document_id, (neighbour.payload or {}).get("chunk_index"))

            if key in taken:
                continue

            taken.add(key)
            found.append(neighbour)

    return found


def _whole_selection(
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
    query_vectors: dict,
    user_id: str,
    document_ids: list[str] | None,
    limit: int = 6,
    min_similarity: float | None = None,
) -> list[dict]:
    """The chunks of the user's documents that best match the question.

    `document_ids` says where to look:

      - a list of ids: only those documents (the user chose them);
      - None:          every finished document of this user;
      - an empty list: nothing is searched. This is deliberately NOT the
                       same as None, so a caller that loses its ids by mistake
                       gets no results instead of the whole library.

    `query_vectors` is the result of embed_query (the question is embedded
    once and shared with the chat memory search). The result has the same
    shape as retrieve_messages: [{"id", "score", "payload"}], best first,
    where `score` is the dense similarity. The payload carries `content`,
    `filename`, `document_id`, `chunk_index`, `section_title`, `page` and
    `timestamp`.

    How the result is built:

      1. Hybrid search (dense + keyword, fused), then every chunk whose dense
         similarity is below the threshold is dropped. The threshold is the
         global MIN_SIMILARITY for the whole library, and the lower
         SELECTED_MIN_SIMILARITY when the user chose the documents (see
         effective_threshold). Chosen documents are still ranked and cut to
         `limit`: the lower threshold only stops weak-but-ranked chunks of a
         document the user asked about from being thrown away.
      2. When more than one document is searched, no document may take more
         than MAX_PER_DOCUMENT of the `limit` places (leftover places are
         given back). At most `limit` chunks are chosen this way.
      3. The chunks next to the best NEIGHBOR_HITS hits are added after them
         (at most 2 * NEIGHBOR_RADIUS each). They are not subject to the
         threshold: they come with a relevant hit, as its surrounding text.
         They have their own similarity as `score`.
    """

    if not user_id:
        raise ValueError("user_id is required")

    # Searching "nothing" is not the same as searching everything (see above)
    if document_ids is not None and not document_ids:
        return []

    # No document was ever ingested: behave as "no documents", never crash
    if not _collection_exists():
        return []

    search_all = document_ids is None
    threshold = effective_threshold(document_ids, min_similarity)

    # The user chose the documents, so the scope is already known: no chunk is
    # refused for a low similarity. Only a whole-library search (nothing
    # chosen), or a caller that passes min_similarity itself, is checked.
    check_similarity = search_all or min_similarity is not None
    factor = ALL_DOCUMENTS_CANDIDATE_FACTOR if search_all else CANDIDATE_FACTOR
    candidates = limit * factor

    dense_vector = query_vectors["dense"]

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
    sparse_vector = models.SparseVector(
        indices=query_vectors["sparse"]["indices"],
        values=query_vectors["sparse"]["values"],
    )

    query_filter = build_filter(user_id, document_ids, ready_only=True)

    results = client.query_points(
        collection_name=CHUNKS_COLLECTION,
        prefetch=[
            models.Prefetch(
                query=dense_vector,
                using="dense",
                limit=candidates,
                filter=query_filter,
            ),
            models.Prefetch(
                query=sparse_vector,
                using="sparse",
                limit=candidates,
                filter=query_filter,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        query_filter=query_filter,
        limit=candidates,
        with_payload=True,
        with_vectors=["dense"],  # needed to measure the similarity
    )

    ranked: list = []  # (point, similarity), best match first
    dropped: list[float] = []

    for point in results.points:
        stored = _dense_vector_of(point)
        similarity = _dot(dense_vector, stored) if stored else 0.0

        if check_similarity and similarity < threshold:
            dropped.append(similarity)
            continue

        ranked.append((point, similarity))

    several_documents = search_all or len(document_ids) > 1
    per_document = MAX_PER_DOCUMENT if several_documents else limit
    chosen = _select(ranked, limit, per_document)

    kept = [_result_item(point, similarity) for point, similarity in chosen]

    # The text around the best hits. An extra, so a failure here only means
    # the answer is written without it.
    extra: list[dict] = []

    try:
        taken = {
            (
                (point.payload or {}).get("document_id"),
                (point.payload or {}).get("chunk_index"),
            )
            for point, _similarity in chosen
        }

        for neighbour in _neighbours(user_id, chosen, taken):
            stored = _dense_vector_of(neighbour)
            similarity = _dot(dense_vector, stored) if stored else 0.0
            extra.append(_result_item(neighbour, similarity))
    except Exception:
        logger.exception("Neighbouring chunks could not be read; continuing without them")

    logger.info(
        "Document retrieval (%s): kept %d + %d neighbours from %d document(s), "
        "dropped %d below %.2f (best dropped: %s)",
        "all documents" if search_all else "chosen documents",
        len(kept),
        len(extra),
        len({item["payload"]["document_id"] for item in kept + extra}),
        len(dropped),
        threshold,
        f"{max(dropped):.2f}" if dropped else "-",
    )

    return kept + extra


def list_documents(user_id: str, limit: int = MAX_LISTED_DOCUMENTS) -> list[dict]:
    """The finished documents of this user: [{"document_id", "filename"}], by name.

    One chunk per document is read (chunk number 0), so this stays cheap. Used
    to tell the model which documents exist when the user chose none, and to
    skip the document search when the user has no documents at all.
    """

    if not user_id:
        raise ValueError("user_id is required")

    if not _collection_exists():
        return []

    query_filter = build_filter(user_id, None, ready_only=True)
    query_filter.must.append(
        models.FieldCondition(key="chunk_index", match=models.MatchValue(value=0))
    )

    points, _offset = client.scroll(
        collection_name=CHUNKS_COLLECTION,
        scroll_filter=query_filter,
        limit=limit,
        with_payload=["document_id", "filename"],
        with_vectors=False,
    )

    names: dict[str, str] = {}

    for point in points:
        payload = point.payload or {}
        document_id = payload.get("document_id")

        if document_id and document_id not in names:
            names[document_id] = _plain(payload.get("filename")) or "document"

    documents = [
        {"document_id": document_id, "filename": filename}
        for document_id, filename in names.items()
    ]
    documents.sort(key=lambda document: document["filename"].lower())

    return documents