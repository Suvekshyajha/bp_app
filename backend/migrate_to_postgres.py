"""One-time copy of the three SQLite databases into Postgres.

Stop the app first. Then, from the backend folder (PowerShell):

    $env:DATABASE_URL = "postgresql://..."
    .venv\\Scripts\\python.exe migrate_to_postgres.py

The SQLite files are only read. Rows that are already in Postgres are skipped,
so running it again after a failure is safe. Do not run it again after the app
has been used with Postgres: anything deleted there since would be copied back.

Encrypted values are copied as they are, so the app that reads Postgres needs
the same CHAT_ENCRYPTION_KEYS.
"""

import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
SERVICES = ("orchestrator", "retriever", "ingestion")
BATCH_SIZE = 500


def copy_service(service: str) -> None:
    """Runs in its own process: the services have modules with the same names."""

    folder = BACKEND / service
    os.chdir(folder)
    sys.path.insert(0, str(folder))

    from sqlalchemy import DateTime, MetaData, func, select
    from sqlalchemy.dialects.postgresql import insert

    from common.database import create_schema
    from database import DATABASE_PATH, Base, engine
    import models  # noqa: F401  (registers the tables)

    if engine.dialect.name != "postgresql":
        raise SystemExit("DATABASE_URL is not set, so there is nothing to copy into.")

    source_path = Path(DATABASE_PATH).resolve()

    if not source_path.exists():
        print(f"{service}: {source_path.name} not found, skipped")
        return

    create_schema(engine, Base.metadata)

    # The tables as Postgres has them, with plain types: encrypted text must be
    # copied unchanged, not encrypted a second time
    target = MetaData()
    names = [table.name for table in Base.metadata.sorted_tables]  # parents first
    target.reflect(bind=engine, only=names)

    source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row

    try:
        for name in names:
            table = target.tables[name]
            present = {row["name"] for row in source.execute(f"PRAGMA table_info({name})")}

            if not present:
                print(f"{service}: no table '{name}' in {source_path.name}, skipped")
                continue

            columns = [c.name for c in table.columns if c.name in present]
            times = {c.name for c in table.columns if isinstance(c.type, DateTime)}

            # Postgres checks foreign keys, so rows without a parent are left out
            parents = [
                (key.parent.name, key.column.table.name, key.column.name)
                for key in table.foreign_keys
            ]
            known = {}

            with engine.connect() as connection:
                for child, parent_table, parent_column in parents:
                    parent = target.tables[parent_table]
                    known[child] = set(
                        connection.execute(select(parent.c[parent_column])).scalars()
                    )

            read = copied = orphans = 0
            cursor = source.execute(f"SELECT {', '.join(columns)} FROM {name}")

            while batch := cursor.fetchmany(BATCH_SIZE):
                rows = []

                for record in batch:
                    row = dict(record)
                    read += 1

                    if any(row[child] not in ids for child, ids in known.items()):
                        orphans += 1
                        continue

                    for column in times & row.keys():
                        if row[column] is not None:
                            row[column] = datetime.fromisoformat(row[column])

                    rows.append(row)

                if rows:
                    with engine.begin() as connection:
                        result = connection.execute(
                            insert(table)
                            .on_conflict_do_nothing()
                            .returning(*table.primary_key.columns),
                            rows,
                        )
                        copied += len(result.fetchall())

            with engine.connect() as connection:
                total = connection.execute(
                    select(func.count()).select_from(table)
                ).scalar_one()

            note = f", {orphans} without a parent row left out" if orphans else ""
            print(
                f"{service}: {name}: read {read}, copied {copied}, "
                f"Postgres now holds {total}{note}"
            )

    finally:
        source.close()
        engine.dispose()


def main() -> None:
    if len(sys.argv) == 2:
        copy_service(sys.argv[1])
        return

    if not os.getenv("DATABASE_URL", "").strip():
        raise SystemExit("Set DATABASE_URL to the Postgres address first.")

    for service in SERVICES:
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), service])

        if done.returncode != 0:
            raise SystemExit(f"Stopped: copying the {service} database failed.")

    print("Done.")


if __name__ == "__main__":
    main()
