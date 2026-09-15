"""
Shared pgvector-backed storage for schema_rag.py and example_rag.py.

This replaces the two Qdrant collections this project used to run
(`private_data_assistant_schema` and `private_data_assistant_examples`)
with two tables - `schema_chunks` and `example_chunks` - in this app's OWN
Postgres database, using the `vector` extension (see the Alembic migration
`backend/alembic/versions/<...>_pgvector_schema_and_example_chunks.py`,
which runs `CREATE EXTENSION IF NOT EXISTS vector` and creates both
tables). Vercel Postgres (Neon-backed) supports `vector` natively, so this
means the whole application - metadata AND embeddings - lives in exactly
one managed database, with no second vector service to provision.

Isolation, same reasoning as the rest of app/engine/ (see
app/engine/__init__.py's contract): this module reads `DATABASE_URL`
directly from `os.environ` rather than importing `app.core.config.Settings`
or `app.db.session`, and defines its own SQLAlchemy Core `Table` objects on
a private `MetaData()` rather than importing `app.models` / `app.db.
base_class.Base`. It still ends up pointed at the exact same Postgres
database as the rest of the app (both ultimately read the same
DATABASE_URL from the same .env / Vercel env var), it just never imports
that side of the app - so this package keeps zero dependency on it and the
isolation contract holds. The two tables are created by an Alembic
migration (raw `op.create_table`, not `Base.metadata.create_all`) for the
same reason: this module's Table objects exist only to build queries here,
not to be autogeneration's source of truth.

Both schema_rag.py and example_rag.py keep their pre-existing public
function signatures (`upsert_table_points`, `search_schema`,
`delete_connection_schema`, `index_example`, etc.) - only their storage
backend changed, so app/api/connections.py and app/api/messages.py did not
need to change at all.

Cosine distance, not Qdrant's cosine SIMILARITY: pgvector's `<=>` operator
(exposed here via `Vector.cosine_distance()`) returns a DISTANCE in
[0, 2] - 0 for identical direction, 2 for opposite. `1 - distance` is
reported as `score` so callers see the same "higher is more similar"
convention Qdrant's cosine score gave them.
"""

import os
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete as sa_delete,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from app.engine.azure_client import get_embedding_dimensions

# A private MetaData - deliberately NOT app.db.base_class.Base. See the
# module docstring for why.
metadata = MetaData()


def _vector_type() -> Vector:
    # Sized to AZURE_EM_DIMENSIONS (default 1536) - must match whatever the
    # Alembic migration created the column as. A mismatch here is a
    # configuration error the same way a Qdrant collection sized for the
    # wrong embedding model used to be.
    return Vector(get_embedding_dimensions())


schema_chunks = Table(
    "schema_chunks",
    metadata,
    Column("id", String, primary_key=True),
    Column("user_id", Integer, nullable=False),
    Column("connection_id", Integer, nullable=False),
    Column("engine", String, nullable=False),
    Column("table_name", String, nullable=False),
    Column("text", Text, nullable=False),
    Column("embedding", _vector_type(), nullable=False),
)

example_chunks = Table(
    "example_chunks",
    metadata,
    Column("id", String, primary_key=True),
    Column("user_id", Integer, nullable=False),
    Column("connection_id", Integer, nullable=False),
    Column("engine", String, nullable=False),
    Column("question", Text, nullable=False),
    Column("query", Text, nullable=False),
    Column("seeded", Boolean, nullable=False, default=False),
    Column("embedding", _vector_type(), nullable=False),
)


@dataclass(frozen=True)
class VectorHit:
    """One row back from a similarity search, with its payload columns
    flattened alongside the similarity score - deliberately shaped like the
    old Qdrant `ScoredPoint` enough that schema_rag.py/example_rag.py's
    result-building code barely changed."""

    payload: Dict[str, Any]
    score: float


@lru_cache
def get_engine() -> Engine:
    """The SQLAlchemy engine for THIS app's own Postgres database, built
    directly from DATABASE_URL - never through app.db.session (see the
    module docstring's isolation note). `pool_pre_ping` matters more here
    than usual: a serverless function's connection can go stale between
    invocations."""
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set - pgvector-backed schema/example "
            "storage is not configured."
        )
    return create_engine(url, pool_pre_ping=True)


def point_id(*parts: Any) -> str:
    """Deterministic UUID from arbitrary parts - same derivation style the
    old Qdrant-based code used (uuid5 over a colon-joined string), kept
    here so schema_rag.py/example_rag.py's own `_point_id` helpers don't
    have to change their call sites, just delegate to this."""
    joined = ":".join(str(part) for part in parts)
    return str(uuid.uuid5(uuid.NAMESPACE_OID, joined))


def upsert_rows(
    table: Table,
    rows: Sequence[Dict[str, Any]],
    engine: Optional[Engine] = None,
) -> int:
    """INSERT ... ON CONFLICT (id) DO UPDATE for each row - the pgvector
    equivalent of Qdrant's upsert-by-point-id semantics (re-indexing
    overwrites rather than duplicating)."""
    if not rows:
        return 0
    engine = engine or get_engine()
    stmt = pg_insert(table).values(list(rows))
    update_columns = {
        column.name: stmt.excluded[column.name]
        for column in table.columns
        if column.name != "id"
    }
    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_columns)
    with engine.begin() as connection:
        connection.execute(stmt)
    return len(rows)


def delete_rows(
    table: Table,
    user_id: int,
    connection_id: int,
    engine: Optional[Engine] = None,
) -> None:
    """DELETE every row for one (user_id, connection_id) pair - both
    columns are always applied together, mirroring the old Qdrant
    must-filter-on-both isolation rule (see schema_rag.py's module
    docstring for why neither can be dropped)."""
    engine = engine or get_engine()
    stmt = sa_delete(table).where(
        table.c.user_id == user_id, table.c.connection_id == connection_id
    )
    with engine.begin() as connection:
        connection.execute(stmt)


def search_rows(
    table: Table,
    query_embedding: Sequence[float],
    user_id: int,
    connection_id: int,
    top_k: int,
    engine: Optional[Engine] = None,
) -> List[VectorHit]:
    """Nearest-neighbor search by cosine distance, scoped unconditionally
    to (user_id, connection_id) - the pgvector equivalent of Qdrant's
    `must` filter search used by both schema_rag.py and example_rag.py."""
    engine = engine or get_engine()
    vector = list(query_embedding)
    distance = table.c.embedding.cosine_distance(vector)
    payload_columns = [
        column for column in table.columns if column.name not in ("id", "embedding")
    ]
    stmt = (
        select(*payload_columns, distance.label("distance"))
        .where(table.c.user_id == user_id, table.c.connection_id == connection_id)
        .order_by(distance.asc())
        .limit(top_k)
    )
    with engine.connect() as connection:
        result = connection.execute(stmt)
        hits: List[VectorHit] = []
        for row in result.mappings():
            row_dict = dict(row)
            dist = row_dict.pop("distance")
            hits.append(VectorHit(payload=row_dict, score=1.0 - float(dist)))
        return hits


def count_rows(
    table: Table,
    user_id: int,
    connection_id: int,
    engine: Optional[Engine] = None,
) -> int:
    engine = engine or get_engine()
    stmt = select(table.c.id).where(
        table.c.user_id == user_id, table.c.connection_id == connection_id
    )
    with engine.connect() as connection:
        return len(connection.execute(stmt).fetchall())
