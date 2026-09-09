"""DatabaseConnection model - a *registration record* for one external
database the user owns, plus its schema-indexing status.

This row is metadata only. None of the user's actual data is ever copied
into this application's database: the only things stored here are how to
reach their database (host/port/database name/username), their password in
encrypted form (see app/core/crypto.py), and whether the schema has been
introspected and embedded into Qdrant yet. Every answer to a data question
is produced by running a read-only query against their live database at
the moment they ask - see app/engine/db_adapters/.

`user_id` is the non-negotiable isolation boundary, exactly like the
sibling project's Document model: every lookup in app/api/connections.py
filters on it, and app/engine/schema_rag.py additionally applies BOTH
`user_id` and `connection_id` as unconditional Qdrant filters, since
cross-connection schema retrieval is never meaningful (a question about
connection A's tables must never be answered with connection B's schema,
even for the same user).

Schema indexing (introspect -> chunk per table -> embed -> upsert into
Qdrant, via app/engine/schema_rag.py) runs synchronously inside the
registration request for this project's scope - `status` starts at
"pending", moves to "indexing", and lands on "ready" (+
`schema_indexed_at`) or "failed" (+ `error_message`) before the response
is returned. A production deployment would enqueue this onto a background
worker instead - see README.md's "Roadmap / known tradeoffs".

`extra_params` is a free-form JSON bag of engine-specific options that
would otherwise need a column each, e.g.:
  - postgres/mysql/mssql: {"ssl_mode": "require"}
  - mongodb:              {"auth_source": "admin"}
  - sqlite:               {"storage_path": "storage/{user_id}/{connection_id}/database.sqlite"}
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.db.base_class import Base


class DatabaseEngine(str, enum.Enum):
    """Which adapter in app/engine/db_adapters/ handles this connection."""

    postgres = "postgres"
    mysql = "mysql"
    mssql = "mssql"
    sqlite = "sqlite"
    mongodb = "mongodb"


class ConnectionStatus(str, enum.Enum):
    pending = "pending"
    indexing = "indexing"
    ready = "ready"
    failed = "failed"


class DatabaseConnection(Base):
    __tablename__ = "database_connections"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # A user-given label ("Production analytics", "Local test MySQL") -
    # purely for display, never used to build a connection.
    name = Column(String, nullable=False)
    engine = Column(Enum(DatabaseEngine, name="database_engine"), nullable=False)

    # Nullable for sqlite (there is no host/port/username - the database is
    # an uploaded file, see extra_params["storage_path"]).
    host = Column(String, nullable=True)
    port = Column(Integer, nullable=True)
    database_name = Column(String, nullable=False)
    username = Column(String, nullable=True)
    # Fernet ciphertext, never plaintext - and never returned by any API
    # response (app/api/connections.py's ConnectionOut has no field for it
    # at all). Nullable for sqlite / genuinely password-less connections.
    encrypted_password = Column(Text, nullable=True)

    extra_params = Column(JSON, nullable=False, default=dict)

    status = Column(
        Enum(ConnectionStatus, name="connection_status"),
        nullable=False,
        default=ConnectionStatus.pending,
    )
    error_message = Column(Text, nullable=True)
    schema_indexed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    user = relationship("User", back_populates="connections")
    chats = relationship("Chat", back_populates="connection")
