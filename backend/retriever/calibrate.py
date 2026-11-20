import csv
from pathlib import Path

from embeddings import embed_query, embed_messages

# Load labeled pairs from CSV
csv_path = Path(__file__).with_name("pairs.csv")

PAIRS = []

with open(csv_path, "r", encoding="utf-8", newline="") as file:
    reader = csv.DictReader(file)

    for row in reader:
        question = row["question"]
        message = row["message"]
        relevant = row["label"].strip() == "1"

        PAIRS.append((question, message, relevant))


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


rows = []

for question, message, relevant in PAIRS:
    score = dot(
        embed_query(question)["dense"],
        embed_messages([message])[0]["dense"]
    )

    rows.append((score, relevant, question, message))


for score, relevant, question, message in sorted(
    rows, key=lambda x: x[0], reverse=True
):
    print(
        f"{score:.2f}  "
        f"{'RELEVANT' if relevant else 'not     '}  "
        f"{question[:30]} | {message[:30]}"
    )


worst_irrelevant = max(s for s, r, *_ in rows if not r)
worst_relevant = min(s for s, r, *_ in rows if r)

print(f"\nHighest irrelevant: {worst_irrelevant:.2f}")
print(f"Lowest relevant:    {worst_relevant:.2f}")