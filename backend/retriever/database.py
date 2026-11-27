import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from common.database import make_engine


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# The chat service owns this file (conversations and messages).
# Set CHAT_DB_PATH to keep it somewhere else.
DATABASE_PATH = Path(os.getenv("CHAT_DB_PATH", BASE_DIR / "chat.db"))
# DATABASE_URL set: Postgres. Not set: the SQLite file above.
engine = make_engine(DATABASE_PATH)


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


class Base(DeclarativeBase):
    pass



# create_all() makes missing TABLES but never adds a column to a table that
# already exists. Columns added after the first release are listed here and
# added to old databases when the service (or a script) starts.
NEW_COLUMNS = [
    # (table, column, SQL type)
    ("messages", "context", "TEXT"),
]


def migrate_schema() -> None:
    with engine.begin() as connection:
        for table, column, sql_type in NEW_COLUMNS:
            inspector = inspect(connection)

            if not inspector.has_table(table):
                continue

            existing = {item["name"] for item in inspector.get_columns(table)}

            if column not in existing:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
                )
