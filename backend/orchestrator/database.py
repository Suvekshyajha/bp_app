import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("ORCH_DB_PATH", BASE_DIR / "chat.db"))
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=NullPool,  # no pool: every connection is real, closed for real
)


# Runs on every new connection: WAL lets readers and a writer work together
@event.listens_for(engine, "connect")
def set_sqlite_pragmas(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()