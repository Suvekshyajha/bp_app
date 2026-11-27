"""The database connection shared by the three services.

With DATABASE_URL set (a postgresql:// address, for example from Neon) every
service uses that Postgres database. Without it each service keeps its own
SQLite file, which is the default on a developer's machine.

The services use different table names, so they can share one database.
"""

import os
from datetime import timezone
from pathlib import Path

from sqlalchemy import DateTime, create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool
from sqlalchemy.types import TypeDecorator

__all__ = ["UTCDateTime", "make_engine", "create_schema"]

# Any number, as long as every process uses the same one (see create_schema)
SCHEMA_LOCK_ID = 7_421_905


class UTCDateTime(TypeDecorator):
    """A time stored as UTC without a timezone label, in both databases.

    Postgres would otherwise shift an aware time into the connection's time
    zone before dropping the label.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)

        return value


def _postgres_engine(url: str) -> Engine:
    parsed = make_url(url)

    if parsed.get_backend_name() not in ("postgresql", "postgres"):
        # Never put the address in a message: it holds the password
        raise ValueError("DATABASE_URL must start with postgresql://")

    return create_engine(
        parsed.set(drivername="postgresql+psycopg"),
        pool_size=5,
        max_overflow=10,
        # A hosted database drops idle connections (Neon sleeps after a few
        # minutes), so each one is checked before use and replaced if old
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={
            "connect_timeout": 15,  # a sleeping database needs a moment
            # Safe behind a connection pooler such as the "-pooler" address
            "prepare_threshold": None,
        },
    )


def _sqlite_engine(path: Path, busy_timeout_ms: int | None) -> Engine:
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,  # no pool: every connection is real, closed for real
    )

    # Runs on every new connection: WAL lets readers and a writer work together
    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")

        if busy_timeout_ms is not None:
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")

        cursor.close()

    return engine


def make_engine(sqlite_path: Path, busy_timeout_ms: int | None = None) -> Engine:
    """Postgres if DATABASE_URL is set, otherwise the SQLite file given.

    Load the .env file before calling this.
    """

    url = os.getenv("DATABASE_URL", "").strip()

    if url:
        return _postgres_engine(url)

    return _sqlite_engine(sqlite_path, busy_timeout_ms)


def create_schema(engine: Engine, metadata) -> None:
    """Create the missing tables and indexes.

    Several processes start at once. In Postgres a lock makes them take turns,
    because two of them creating the same table at the same moment fails.
    """

    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:id)"), {"id": SCHEMA_LOCK_ID}
            )

        metadata.create_all(bind=connection)

        # create_all skips tables that already exist, so it would never add a
        # new index to an old database
        for table in metadata.sorted_tables:
            for index in table.indexes:
                index.create(bind=connection, checkfirst=True)
