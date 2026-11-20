from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    String,
    Text,
    DateTime,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, index=True)
    username = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)

    conversations = relationship(
        "Conversation",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(
        String,
        ForeignKey("users.id"),
        nullable=False,
    )
    title = Column(String, nullable=True)
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
    )

    user = relationship("User", back_populates="conversations")

    __table_args__ = (
        # Lists a user's chats
        Index("ix_conversations_user", "user_id"),
    )

    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(Base):
    __tablename__ = "messages"

    id = Column(String, primary_key=True, index=True)
    conversation_id = Column(
        String,
        ForeignKey("conversations.id"),
        nullable=False,
    )
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
    )

    conversation = relationship(
        "Conversation",
        back_populates="messages",
    )

    __table_args__ = (
        # Lets a page of one chat's messages be read straight from the index
        Index(
            "ix_messages_conversation_created",
            "conversation_id",
            "created_at",
            "id",
        ),
    )


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