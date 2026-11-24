import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool


BASE_DIR = Path(__file__).resolve().parent

# The ingestion service owns this file (documents and their job state).
# It never shares chat.db. Set INGEST_DB_PATH to keep it somewhere else.
DATABASE_PATH = Path(os.getenv("INGEST_DB_PATH", BASE_DIR / "ingest.db"))
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=NullPool,  # no pool: every connection is real, closed for real
)


# Runs on every new connection. The API and the worker are two processes that
# open this one file, so WAL (readers and a writer together) and a busy
# timeout (wait a moment instead of failing) are both needed.
@event.listens_for(engine, "connect")
def set_sqlite_pragmas(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


class Base(DeclarativeBase):
    pass


# create_all() makes missing TABLES but never adds a column to a table that
# already exists. Columns added after the first release are listed here and
# added to old databases when the service starts.
NEW_COLUMNS: list[tuple[str, str, str]] = [
    # (table, column, SQL type)
]


def migrate_schema() -> None:
    with engine.begin() as connection:
        for table, column, sql_type in NEW_COLUMNS:
            existing = {
                row[1]
                for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
            }

            if existing and column not in existing:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
                )
