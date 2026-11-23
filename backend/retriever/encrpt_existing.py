"""Encrypt the chat text that is already stored as plain text.

Run once, with the chat service stopped, from the retriever folder:

    python encrypt_existing.py            # SQLite only
    python encrypt_existing.py --qdrant   # SQLite and the Qdrant payloads

Safe to run again: values that are already encrypted are skipped.

SQLite keeps old plain text in free pages and in the write-ahead log after
an UPDATE, so the script finishes with a checkpoint and a VACUUM to wipe it.
"""

import sqlite3
import sys

from crypto import encrypt, is_encrypted
from database import DATABASE_PATH

# (table, column) pairs that hold chat text
COLUMNS = [
    ("messages", "content"),
    ("conversations", "title"),
]


def encrypt_sqlite() -> None:
    connection = sqlite3.connect(DATABASE_PATH)

    try:
        for table, column in COLUMNS:
            rows = connection.execute(
                f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall()

            todo = [(row_id, value) for row_id, value in rows if not is_encrypted(value)]

            with connection:
                connection.executemany(
                    f"UPDATE {table} SET {column} = ? WHERE id = ?",
                    [(encrypt(value), row_id) for row_id, value in todo],
                )

            print(f"{table}.{column}: encrypted {len(todo)} of {len(rows)} values")

        # Wipe the old plain text out of the file itself
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")

    finally:
        connection.close()


def encrypt_qdrant() -> None:
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

            if content is not None and not is_encrypted(content):
                client.set_payload(
                    collection_name=COLLECTION_NAME,
                    payload={"content": encrypt(content)},
                    points=[point.id],
                )
                done += 1

        if offset is None:
            break

    print(f"qdrant payloads: encrypted {done} of {total} points")


if __name__ == "__main__":
    encrypt_sqlite()

    if "--qdrant" in sys.argv:
        encrypt_qdrant()