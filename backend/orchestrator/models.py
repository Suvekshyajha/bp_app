from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    String,
    DateTime,
    ForeignKey,
)

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, index=True)
    username = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)


class UserSession(Base):
    """A logged-in browser session.

    The browser holds a random value in an HttpOnly cookie.
    Only its SHA-256 hash is stored here, so a leaked database
    cannot be used to log in.
    """

    __tablename__ = "sessions"

    id = Column(String, primary_key=True)  # sha256 of the cookie value
    user_id = Column(
        String,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at = Column(DateTime, nullable=False)