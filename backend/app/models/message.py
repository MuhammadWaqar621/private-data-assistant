"""Message model - a single turn (user or assistant) within a Chat.

Two columns exist beyond the obvious role/content pair, both nullable and
both only ever set on an assistant turn:

  - `query_sql`: the literal query text that was actually executed against
    the user's database for this turn (SQL for the SQL engines, the JSON
    operation spec for MongoDB - see app/engine/db_adapters/mongodb_adapter.py).
    NULL when the turn involved no `run_query` tool call at all (a greeting,
    or a question answered without touching data). This is a transparency/
    audit record: the user can always see exactly what ran against their
    database to produce an answer, which matters far more here than in a
    document-RAG product, because the assistant is writing queries against
    live systems.
  - `chart_spec`: the rendered chart's spec (type/title/x_field/y_field
    plus the actual columns+rows that were charted) when the agent called
    `render_chart` for this turn. Persisted so reloading the history
    re-renders the same chart instead of losing it - the rows come from the
    query result the backend held server-side, never from anything the
    model made up.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, Enum, ForeignKey, Integer, Text
from sqlalchemy.orm import relationship

from app.db.base_class import Base


class MessageRole(str, enum.Enum):
    user = "user"
    assistant = "assistant"


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(Integer, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(Enum(MessageRole, name="message_role"), nullable=False)
    content = Column(Text, nullable=False)
    query_sql = Column(Text, nullable=True)
    chart_spec = Column(JSON, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    chat = relationship("Chat", back_populates="messages")
