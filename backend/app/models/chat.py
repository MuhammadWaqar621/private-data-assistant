"""Chat model - a conversation thread owned by a user, optionally bound to
one of that user's registered database connections.

`connection_id` is NULLABLE on purpose: a chat exists before the user has
picked a database (the "+ New chat" button can't know which database the
first question will be about), and greetings/small talk must work with no
connection selected at all. app/api/messages.py handles both cases - with
no connection there is simply no schema context to retrieve and no
database to query, so the agent answers directly or tells the user to
connect a database first (see AGENT_SYSTEM_PROMPT in app/engine/rag.py).

Ownership (`user_id`) is what every lookup in the API layer filters on.
Setting `connection_id` (PATCH /api/chats/{id}) additionally verifies the
connection is the caller's OWN - a user can never point their chat at
another account's registered database.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.db.base_class import Base


class Chat(Base):
    __tablename__ = "chats"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False, default="New chat")
    # NULL = no database selected yet for this chat. ON DELETE SET NULL:
    # deleting a connection leaves its chat history intact (the transcript
    # is still worth keeping) but unbinds it, so no message can be sent
    # against a connection that no longer exists.
    connection_id = Column(
        Integer,
        ForeignKey("database_connections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    user = relationship("User", back_populates="chats")
    connection = relationship("DatabaseConnection", back_populates="chats")
    messages = relationship(
        "Message",
        back_populates="chat",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )
