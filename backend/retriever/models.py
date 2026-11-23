from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import relationship

from crypto import EncryptedText
from database import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(String, primary_key=True, index=True)

    # No ForeignKey: users live in the orchestrator's database, and a foreign
    # key cannot point into another SQLite file. The orchestrator has already
    # checked who the user is before it calls this service.
    user_id = Column(String, nullable=False)

    # Encrypted at rest (see crypto.py)
    title = Column(EncryptedText, nullable=True)
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
    )

    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )

    __table_args__ = (
        # Lists a user's chats
        Index("ix_conversations_user", "user_id"),
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

    # Encrypted at rest (see crypto.py)
    content = Column(EncryptedText, nullable=False)
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
    )

    conversation = relationship("Conversation", back_populates="messages")

    __table_args__ = (
        # Lets a page of one chat's messages be read straight from the index
        Index(
            "ix_messages_conversation_created",
            "conversation_id",
            "created_at",
            "id",
        ),
    )