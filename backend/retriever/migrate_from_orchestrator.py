"""One-time copy of conversations and messages from the orchestrator's old
chat.db into the chat service's own database.

Stop both services first, then run from the retriever folder:

    python migrate_from_orchestrator.py ../orchestrator/chat.db

It only copies rows that are not already there, so running it twice is safe.
The old file is not changed.
"""

import sqlite3
import sys

from database import DATABASE_PATH, Base, engine
import models  # noqa: F401  (registers the tables)


def main(old_db_path: str) -> None:
    Base.metadata.create_all(bind=engine)

    connection = sqlite3.connect(DATABASE_PATH)

    try:
        connection.execute("ATTACH DATABASE ? AS old", (f"file:{old_db_path}?mode=ro",))
    except sqlite3.OperationalError:
        # Older sqlite builds need a plain path
        connection.execute("ATTACH DATABASE ? AS old", (old_db_path,))

    with connection:
        conversations = connection.execute(
            """
            INSERT OR IGNORE INTO main.conversations (id, user_id, title, created_at)
            SELECT id, user_id, title, created_at FROM old.conversations
            """
        ).rowcount

        messages = connection.execute(
            """
            INSERT OR IGNORE INTO main.messages (id, conversation_id, role, content, created_at)
            SELECT id, conversation_id, role, content, created_at FROM old.messages
            """
        ).rowcount

    connection.close()

    print(f"Copied {conversations} conversations and {messages} messages into {DATABASE_PATH}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python migrate_from_orchestrator.py <path to old chat.db>")

    main(sys.argv[1])
