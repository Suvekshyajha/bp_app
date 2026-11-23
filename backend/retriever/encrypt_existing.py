"""Encrypt the chat text that is already stored as plain text.

Run once, with the chat service stopped, from the retriever folder:

    python encrypt_existing.py            # SQLite only
    python encrypt_existing.py --qdrant   # SQLite and the Qdrant payloads
    python encrypt_existing.py --rekey    # also re-encrypt with the newest key
                                          # (after adding a new key in front)

Safe to run again: values that are already encrypted are skipped.

SQLite keeps old plain text in free pages and in the write-ahead log after
an UPDATE, so the script finishes with a checkpoint and a VACUUM to wipe it.
"""

import sqlite3
import sys

from crypto import check_configured, encrypt, is_encrypted, rekey
from database import DATABASE_PATH, Base, engine, migrate_schema
import models  # noqa: F401  (registers the tables)

# (table, column) pairs that hold chat text
COLUMNS = [
    ("messages", "content"),
    ("messages", "context"),
    ("conversations", "title"),
]


def _fix(value: str, rotate: bool) -> str | None:
    """The value to store, or None if it is already right."""

    if not is_encrypted(value):
        return encrypt(value)

    return rekey(value) if rotate else None


def encrypt_sqlite(rotate: bool = False) -> None:
    connection = sqlite3.connect(DATABASE_PATH)

    try:
        for table, column in COLUMNS:
            rows = connection.execute(
                f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall()

            todo = [
                (new, row_id)
                for row_id, value in rows
                if (new := _fix(value, rotate)) is not None
            ]

            with connection:
                connection.executemany(
                    f"UPDATE {table} SET {column} = ? WHERE id = ?",
                    todo,
                )

            print(f"{table}.{column}: encrypted {len(todo)} of {len(rows)} values")

        # Wipe the old plain text out of the file itself
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")

    finally:
        connection.close()


def encrypt_qdrant(rotate: bool = False) -> None:
    from qdrant_store import COLLECTION_NAME, client

    done = total = 0
    offset = None

    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )

        for point in points:
            total += 1
            content = (point.payload or {}).get("content")

            new = None if content is None else _fix(content, rotate)

            if new is not None:
                client.set_payload(
                    collection_name=COLLECTION_NAME,
                    payload={"content": new},
                    points=[point.id],
                )
                done += 1

        if offset is None:
            break

    print(f"qdrant payloads: encrypted {done} of {total} points")


if __name__ == "__main__":
    check_configured()

    # Old databases may lack the messages.context column
    Base.metadata.create_all(bind=engine)
    migrate_schema()

    rotate = "--rekey" in sys.argv

    encrypt_sqlite(rotate)

    if "--qdrant" in sys.argv:
        encrypt_qdrant(rotate)