import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from common.database import make_engine


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("ORCH_DB_PATH", BASE_DIR / "chat.db"))
# DATABASE_URL set: Postgres. Not set: the SQLite file above.
engine = make_engine(DATABASE_PATH)


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