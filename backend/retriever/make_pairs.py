"""Step 1: build a sheet of question/message pairs from your real chats.

    python make_pairs.py ../orchestrator/chat.db

Writes pairs.csv. Open it in Excel and fill the `label` column:
    1 = the message would genuinely help answer the question
    0 = it would not
"""

import csv
import random
import re
import sqlite3
import sys

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "chat.db"
QUESTIONS = 12            # how many of your messages to use as questions
SAME_CHAT_PER_QUESTION = 2
OTHER_CHAT_PER_QUESTION = 1

random.seed(7)  # same sheet every run


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:300]


rows = sqlite3.connect(DB_PATH).execute(
    "SELECT id, conversation_id, role, content FROM messages "
    "ORDER BY created_at, id"
).fetchall()

# Only earlier messages can be retrieved, so pair a question with what
# came BEFORE it in its own chat (the same rule the app follows).
questions = [
    (i, r) for i, r in enumerate(rows)
    if r[2] == "user" and any(o[1] == r[1] for o in rows[:i])
]
random.shuffle(questions)

pairs = []

for index, (qid, chat, role, text) in questions[:QUESTIONS]:
    earlier_same = [o for o in rows[:index] if o[1] == chat]
    other_chats = [o for o in rows if o[1] != chat]

    picked = random.sample(earlier_same, min(SAME_CHAT_PER_QUESTION, len(earlier_same)))
    picked += random.sample(other_chats, min(OTHER_CHAT_PER_QUESTION, len(other_chats)))

    for _, other_chat, other_role, other_text in picked:
        pairs.append([
            clean(text),
            clean(other_text),
            "same chat" if other_chat == chat else "other chat",
            other_role,
            "",   # <- you fill this: 1 or 0
        ])

with open("pairs.csv", "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.writer(f)
    writer.writerow(["question", "message", "where", "message_role", "label"])
    writer.writerows(pairs)

print(f"Wrote {len(pairs)} pairs to pairs.csv. Fill the label column (1 or 0).")