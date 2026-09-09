"""
Schema-RAG: turning a registered database's SCHEMA (not its data) into
searchable vectors, so the model can be shown only the handful of tables a
question is actually about.

Why this exists at all: a real database can have hundreds of tables and
thousands of columns - far more than fits in a prompt, and mostly
irrelevant to any single question. So the schema is chunked one-chunk-per-
table, embedded, and stored in Qdrant; at question time the question is
embedded and the top-K nearest table chunks are pulled back as context for
the text-to-query call. It is exactly the sibling project's document-RAG
pipeline with tables in place of pages.

Configuration is read directly from environment variables (QDRANT_URL,
optional QDRANT_API_KEY for Qdrant Cloud, QDRANT_COLLECTION) - see
azure_client.py's module docstring for why this package avoids importing
app.core.config.

--------------------------------------------------------------------------
ISOLATION - the single most important property in this project
--------------------------------------------------------------------------
Every point carries `user_id` AND `connection_id` in its payload, and
`search_schema()` applies BOTH as unconditional `must` filter conditions on
every single search. Neither is ever optional:

  - `user_id`, for the obvious reason: one account's database schema must
    never be retrievable by another. (Schema is not innocuous - table and
    column names routinely leak business model, customer names, and
    internal terminology.)
  - `connection_id`, because unlike the sibling project's cross-chat
    document search, cross-connection schema retrieval is never
    meaningful: writing a query against connection A using connection B's
    table names produces a query that cannot run at best, and reads the
    wrong system at worst.

`connection_id` is a per-user autoincrement-shaped integer, so two
different users routinely hold the SAME numeric connection_id - which is
precisely why `user_id` cannot be dropped just because `connection_id` is
present. See tests/test_schema_qdrant_isolation.py, which proves both
directions against a real Qdrant instance.

Do not weaken either condition (e.g. to a `should` clause) without a very
good reason.
"""

import os
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from app.engine.azure_client import embed_texts, get_embedding_dimensions
from app.engine.db_adapters.base import TableSchema

COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "private_data_assistant_schema")

DEFAULT_TOP_K = 8

# Hard cap on how much of one table's rendered text is embedded/stored. A
# 300-column table would otherwise blow past the embedding model's input
# limit; truncating keeps the most important part (the table name and the
# first columns, which is how schemas are conventionally ordered).
MAX_CHUNK_CHARS = 6000


@dataclass(frozen=True)
class TableDocument:
    """One embeddable chunk: a table, rendered as text."""

    table_name: str
    text: str


@dataclass(frozen=True)
class SchemaSearchResult:
    table_name: str
    text: str
    engine: str
    score: float


@lru_cache
def get_qdrant_client() -> QdrantClient:
    url = (os.getenv("QDRANT_URL") or "http://localhost:6333").strip()
    api_key = (os.getenv("QDRANT_API_KEY") or "").strip() or None
    return QdrantClient(url=url, api_key=api_key)


def ensure_collection(client: Optional[QdrantClient] = None) -> None:
    """Create the collection if it doesn't exist yet, sized for the
    configured embedding model (AZURE_EM_DIMENSIONS, default 1536)."""
    client = client or get_qdrant_client()
    existing = {collection.name for collection in client.get_collections().collections}
    if COLLECTION_NAME in existing:
        return
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=qmodels.VectorParams(
            size=get_embedding_dimensions(),
            distance=qmodels.Distance.COSINE,
        ),
    )


# --- chunking ---------------------------------------------------------------


def _render_sample_rows(table: TableSchema) -> str:
    """The sample rows as a small Markdown table.

    Real values matter more than they look: they show the model the actual
    format of a date column, the exact spelling of a status enum, whether
    an amount is in cents or dollars - all things it would otherwise have
    to guess at when writing a WHERE clause."""
    if not table.sample_rows:
        return ""

    headers: List[str] = []
    for row in table.sample_rows:
        for key in row:
            if key not in headers:
                headers.append(key)
    if not headers:
        return ""

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in table.sample_rows:
        cells = []
        for header in headers:
            value = row.get(header)
            text = "" if value is None else str(value)
            # Keep the markdown table intact and each cell readable.
            text = text.replace("|", "\\|").replace("\n", " ")
            cells.append(text[:80])
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def build_table_documents(tables: Sequence[TableSchema]) -> List[TableDocument]:
    """One text chunk per table - the exact text that gets embedded.

    Deliberately written as prose-ish Markdown rather than raw DDL: the
    embedding model matches a natural-language question ("how many orders
    shipped late last month?") against natural-language-ish text far better
    than against `CREATE TABLE` syntax, and the same text is what the
    text-to-query call reads back as context.
    """
    documents: List[TableDocument] = []
    for table in tables:
        lines = [f"Table: {table.name}", "Columns:"]
        if table.columns:
            for column in table.columns:
                markers = []
                if column.is_pk:
                    markers.append("primary key")
                if column.is_fk:
                    markers.append(
                        f"foreign key -> {column.references}" if column.references else "foreign key"
                    )
                markers.append("nullable" if column.nullable else "not null")
                lines.append(f"  - {column.name} ({column.type}) [{', '.join(markers)}]")
        else:
            lines.append("  (no column information available)")

        sample = _render_sample_rows(table)
        if sample:
            lines.append("")
            lines.append(f"Sample rows (up to {len(table.sample_rows)}):")
            lines.append(sample)

        text = "\n".join(lines)
        if len(text) > MAX_CHUNK_CHARS:
            text = text[:MAX_CHUNK_CHARS] + "\n... (schema truncated)"
        documents.append(TableDocument(table_name=table.name, text=text))
    return documents


# --- storage ------------------------------------------------------------------


def _point_id(user_id: int, connection_id: int, table_name: str) -> str:
    """Deterministic UUID from (user, connection, table), so re-indexing a
    connection overwrites its previous points instead of accumulating
    duplicates. The user_id is part of the key as well as the payload:
    two users' identically-named tables on identically-numbered
    connections must never collide onto one point."""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_OID,
            f"user:{user_id}:connection:{connection_id}:table:{table_name}",
        )
    )


def upsert_table_points(
    user_id: int,
    connection_id: int,
    engine_name: str,
    documents: Sequence[TableDocument],
    embeddings: Sequence[Sequence[float]],
    client: Optional[QdrantClient] = None,
) -> int:
    """Low-level upsert: takes embeddings that have already been computed.

    Split out from `index_connection_schema()` below so the isolation tests
    can write points with cheap synthetic vectors instead of calling Azure -
    the filtering behavior under test doesn't depend on the vectors at all.
    """
    if len(documents) != len(embeddings):
        raise ValueError("documents and embeddings must be the same length")

    client = client or get_qdrant_client()
    ensure_collection(client)

    points = [
        qmodels.PointStruct(
            id=_point_id(user_id, connection_id, document.table_name),
            vector=list(embedding),
            payload={
                "user_id": user_id,
                "connection_id": connection_id,
                # The DB engine type travels with the point so
                # retrieve_relevant_schema()'s context can tell the model
                # which dialect to write (T-SQL vs Postgres vs a Mongo
                # operation spec) without a second lookup.
                "engine": engine_name,
                "table_name": document.table_name,
                "text": document.text,
            },
        )
        for document, embedding in zip(documents, embeddings)
    ]
    if not points:
        return 0

    client.upsert(collection_name=COLLECTION_NAME, points=points)
    return len(points)


def index_connection_schema(
    user_id: int,
    connection_id: int,
    engine_name: str,
    tables: Sequence[TableSchema],
    client: Optional[QdrantClient] = None,
) -> int:
    """Chunk -> embed -> upsert one connection's whole schema. Returns the
    number of points written.

    Callers must have checked `azure_client.ai_configured()` first (same
    contract as everything else in this engine)."""
    documents = build_table_documents(tables)
    if not documents:
        return 0
    embeddings = embed_texts([document.text for document in documents])
    return upsert_table_points(
        user_id=user_id,
        connection_id=connection_id,
        engine_name=engine_name,
        documents=documents,
        embeddings=embeddings,
        client=client,
    )


def delete_connection_schema(
    user_id: int,
    connection_id: int,
    client: Optional[QdrantClient] = None,
) -> None:
    """Remove every point for one connection - called when a connection is
    deleted, and before re-indexing on a schema refresh (so a table that
    has since been dropped doesn't linger in the index).

    Filters on user_id as well as connection_id: a bug that passed the
    wrong connection_id must not be able to delete a different account's
    points."""
    client = client or get_qdrant_client()
    # Same reason as upsert_table_points(): on a brand-new Qdrant instance
    # the collection doesn't exist yet, and this is called (from
    # provision_connection()) BEFORE index_connection_schema() ever gets a
    # chance to create it via ensure_collection() - so the very first
    # connection any user ever registers would otherwise fail here with a
    # 404 "Collection doesn't exist" before indexing is ever attempted.
    ensure_collection(client)
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=qmodels.FilterSelector(
            filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="user_id", match=qmodels.MatchValue(value=user_id)
                    ),
                    qmodels.FieldCondition(
                        key="connection_id", match=qmodels.MatchValue(value=connection_id)
                    ),
                ]
            )
        ),
    )


# --- retrieval ------------------------------------------------------------------


def search_schema(
    query_embedding: Sequence[float],
    user_id: int,
    connection_id: int,
    top_k: int = DEFAULT_TOP_K,
    client: Optional[QdrantClient] = None,
) -> List[SchemaSearchResult]:
    """Vector search over schema chunks, scoped to (user_id, connection_id).

    BOTH conditions are REQUIRED `must` filters on every call - there is no
    parameter to relax either one, by design. A table chunk stored under a
    different user_id, or under a different connection_id, is never
    returned here no matter how similar its embedding is to the query. See
    the module docstring.
    """
    client = client or get_qdrant_client()

    query_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=user_id)),
            qmodels.FieldCondition(
                key="connection_id", match=qmodels.MatchValue(value=connection_id)
            ),
        ]
    )

    hits = client.search(
        collection_name=COLLECTION_NAME,
        query_vector=list(query_embedding),
        query_filter=query_filter,
        limit=top_k,
    )

    return [
        SchemaSearchResult(
            table_name=(hit.payload or {}).get("table_name", ""),
            text=(hit.payload or {}).get("text", ""),
            engine=(hit.payload or {}).get("engine", ""),
            score=hit.score,
        )
        for hit in hits
    ]


def retrieve_relevant_schema(
    user_id: int,
    connection_id: int,
    question: str,
    top_k: int = DEFAULT_TOP_K,
    client: Optional[QdrantClient] = None,
) -> str:
    """Embed `question`, pull the top-K matching table chunks for this
    (user, connection), and join them into one context string for the
    text-to-query prompt.

    Returns "" when nothing matches or the connection has no points yet
    (not-yet-indexed, or an empty database) - the caller passes that empty
    string straight through, and AGENT_SYSTEM_PROMPT tells the model what
    to do when it has no schema to work from. Importantly this touches
    Qdrant ONLY: answering a question never re-reads the user's live
    database schema, and never touches their data until the model actually
    asks to run a query.

    Note there is no score threshold here, unlike the sibling project's
    document retrieval. Schema chunks are not "facts to ground an answer
    in" - they are the map the model needs to write any query at all, and a
    weak-but-best match is far more useful than nothing.
    """
    if not question.strip():
        return ""

    embeddings = embed_texts([question])
    if not embeddings:
        return ""

    results = search_schema(
        embeddings[0],
        user_id=user_id,
        connection_id=connection_id,
        top_k=top_k,
        client=client,
    )
    if not results:
        return ""

    return "\n\n---\n\n".join(result.text for result in results if result.text)


def schema_stats(
    user_id: int, connection_id: int, client: Optional[QdrantClient] = None
) -> Dict[str, Any]:
    """How many table chunks are indexed for one connection - used by the
    API layer to report indexing results, and handy when debugging "why
    doesn't it know about my table"."""
    client = client or get_qdrant_client()
    result = client.count(
        collection_name=COLLECTION_NAME,
        count_filter=qmodels.Filter(
            must=[
                qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=user_id)),
                qmodels.FieldCondition(
                    key="connection_id", match=qmodels.MatchValue(value=connection_id)
                ),
            ]
        ),
        exact=True,
    )
    return {"indexed_tables": result.count}
