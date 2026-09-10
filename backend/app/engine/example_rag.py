"""
Few-shot example-query RAG: a second Qdrant collection, alongside
schema_rag's table chunks, that stores QUESTION -> QUERY pairs so the
model has concrete worked examples of the join style a connection's schema
actually needs - not just column lists.

Two sources feed it, both write through the same `index_example()`:

  1. **Seeded at registration** (`seed_fk_join_examples()`, called from
     app/api/connections.py's `provision_connection()` right after schema
     indexing succeeds): one example per foreign-key relationship,
     generated deterministically from the introspected schema - no LLM
     call, no guessing. A child table with a FK to a parent table becomes
     "list each <child> together with its related <parent>", with the
     literal correct JOIN already written using the real PK/FK column
     names. This guarantees at least one genuine multi-table pattern per
     relationship exists before any user ever asks a question.
  2. **Captured from real usage** (`index_example()` called directly from
     app/api/messages.py after a turn where run_query actually succeeded):
     the user's own question, paired with the exact query that answered it.
     This is what makes the index improve with use - a hard cross-domain
     question that this connection's users ask repeatedly becomes easier
     for the model to answer correctly next time, once someone has asked
     it (and it succeeded) once.

Retrieval (`retrieve_relevant_examples()`) mirrors schema_rag.py exactly:
embed the new question, search filtered by BOTH `user_id` and
`connection_id` (unconditional - see schema_rag.py's isolation docstring,
identical reasoning applies here), top-K nearest by similarity to past
QUESTIONS (not past queries - a question is what a new question should be
compared against).

Configuration is read directly from environment variables, same reason as
schema_rag.py / azure_client.py (see app/engine/__init__.py).
"""

import os
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional, Sequence

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from app.engine.azure_client import embed_texts, get_embedding_dimensions
from app.engine.db_adapters.base import TableSchema

COLLECTION_NAME = os.getenv("QDRANT_EXAMPLES_COLLECTION", "private_data_assistant_examples")

DEFAULT_TOP_K = 3

# Hard cap on how many FK-derived examples one connection seeds at
# registration time. A very wide schema can have dozens of foreign keys;
# capping keeps registration fast and keeps the example index focused on
# the most structurally important relationships rather than every one.
MAX_SEEDED_EXAMPLES = 12


@dataclass(frozen=True)
class ExampleDocument:
    question: str
    query: str


@lru_cache
def _client() -> QdrantClient:
    # Deliberately duplicated from schema_rag.py rather than imported: this
    # keeps example_rag.py independently readable and independently
    # testable, matching the rest of app/engine/'s style of small,
    # self-contained modules.
    url = (os.getenv("QDRANT_URL") or "http://localhost:6333").strip()
    api_key = (os.getenv("QDRANT_API_KEY") or "").strip() or None
    return QdrantClient(url=url, api_key=api_key)


def ensure_collection(client: Optional[QdrantClient] = None) -> None:
    client = client or _client()
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


def _point_id(user_id: int, connection_id: int, question: str) -> str:
    """Deterministic UUID from (user, connection, normalized question), so
    asking the same question twice OVERWRITES the stored example (with
    whatever query most recently answered it) instead of accumulating
    duplicates that would all rank equally in a future search."""
    normalized = " ".join(question.strip().lower().split())
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_OID,
            f"user:{user_id}:connection:{connection_id}:example:{normalized}",
        )
    )


def index_example(
    user_id: int,
    connection_id: int,
    engine_name: str,
    question: str,
    query: str,
    client: Optional[QdrantClient] = None,
) -> bool:
    """Embed `question` and store it alongside `query` as one example
    point. Returns False (without raising) for blank input - callers treat
    this as best-effort and never let a failure here break a chat turn."""
    question = (question or "").strip()
    query = (query or "").strip()
    if not question or not query:
        return False

    client = client or _client()
    ensure_collection(client)

    embeddings = embed_texts([question])
    if not embeddings:
        return False

    point = qmodels.PointStruct(
        id=_point_id(user_id, connection_id, question),
        vector=list(embeddings[0]),
        payload={
            "user_id": user_id,
            "connection_id": connection_id,
            "engine": engine_name,
            "question": question,
            "query": query,
        },
    )
    client.upsert(collection_name=COLLECTION_NAME, points=[point])
    return True


def delete_connection_examples(
    user_id: int,
    connection_id: int,
    client: Optional[QdrantClient] = None,
) -> None:
    """Remove every example for one connection - called on connection
    delete, and before re-seeding on a schema re-index (mirrors
    schema_rag.delete_connection_schema exactly, same reasoning)."""
    client = client or _client()
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


def retrieve_relevant_examples(
    user_id: int,
    connection_id: int,
    question: str,
    top_k: int = DEFAULT_TOP_K,
    client: Optional[QdrantClient] = None,
) -> str:
    """Embed `question`, pull the top-K nearest past QUESTIONS for this
    (user, connection), and render each as a "Q / Query" pair for the
    text-to-query prompt. Returns "" when nothing is indexed yet - this is
    the common case for a brand new connection with no FK relationships and
    no usage history, and callers pass the empty string straight through
    (there is no example-context placeholder message, unlike schema_rag's
    "none matched" case, because examples are a helpful nudge, not
    something the model needs to be told is missing)."""
    question = (question or "").strip()
    if not question:
        return ""

    client = client or _client()
    embeddings = embed_texts([question])
    if not embeddings:
        return ""

    query_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(key="user_id", match=qmodels.MatchValue(value=user_id)),
            qmodels.FieldCondition(
                key="connection_id", match=qmodels.MatchValue(value=connection_id)
            ),
        ]
    )

    try:
        hits = client.search(
            collection_name=COLLECTION_NAME,
            query_vector=list(embeddings[0]),
            query_filter=query_filter,
            limit=top_k,
        )
    except Exception:  # noqa: BLE001 - collection may not exist yet on a brand-new deployment
        return ""

    blocks = []
    for hit in hits:
        payload = hit.payload or {}
        q = payload.get("question", "")
        query_text = payload.get("query", "")
        if not q or not query_text:
            continue
        blocks.append(f"Q: {q}\nQuery: {query_text}")

    return "\n\n---\n\n".join(blocks)


# --- FK-derived seeding -------------------------------------------------------


def _find_pk_column(table: TableSchema) -> Optional[str]:
    for column in table.columns:
        if column.is_pk:
            return column.name
    return None


def _sql_join_example(
    child: TableSchema,
    child_fk_column: str,
    parent_name: str,
    parent_pk: str,
    engine_name: str,
) -> ExampleDocument:
    quote = {
        "mssql": ("[", "]"),
    }.get(engine_name, ("", ""))
    lq, rq = quote

    def ident(name: str) -> str:
        return f"{lq}{name}{rq}"

    question = f"List each {child.name} row together with its related {parent_name} row."
    query = (
        f"SELECT c.*, p.*\n"
        f"FROM {ident(child.name)} c\n"
        f"JOIN {ident(parent_name)} p ON c.{ident(child_fk_column)} = p.{ident(parent_pk)}"
    )
    if engine_name == "mssql":
        query = query.replace("SELECT c.*, p.*", "SELECT TOP (50) c.*, p.*")
    else:
        query += "\nLIMIT 50"
    return ExampleDocument(question=question, query=query)


def _mongo_join_example(
    child: TableSchema, child_fk_field: str, parent_name: str, parent_pk: str
) -> ExampleDocument:
    question = f"List each {child.name} document together with its related {parent_name} document."
    pipeline = [
        {
            "$lookup": {
                "from": parent_name,
                "localField": child_fk_field,
                "foreignField": parent_pk,
                "as": f"{parent_name}_doc",
            }
        },
        {"$limit": 50},
    ]
    query = (
        '{"operation": "aggregate", "collection": "'
        + child.name
        + '", "pipeline": '
        + str(pipeline).replace("'", '"')
        + "}"
    )
    return ExampleDocument(question=question, query=query)


def build_fk_join_documents(
    tables: Sequence[TableSchema], engine_name: str
) -> List[ExampleDocument]:
    """Deterministically derive up to MAX_SEEDED_EXAMPLES join examples,
    one per foreign-key relationship, from already-introspected schema -
    no LLM call, so this never fails or costs an extra round trip. Pure
    function, directly unit-testable.

    Each FK column's `references` is "other_table.other_column" (see
    ColumnSchema in db_adapters/base.py). A relationship is only usable if
    the referenced table is actually present in `tables` (it always should
    be, but a partial introspection failure elsewhere must not raise here)
    and, for SQL engines, the parent table has a discoverable primary key
    column to join on.
    """
    by_name: Dict[str, TableSchema] = {table.name: table for table in tables}
    documents: List[ExampleDocument] = []

    for table in tables:
        for column in table.columns:
            if len(documents) >= MAX_SEEDED_EXAMPLES:
                return documents
            if not column.is_fk or not column.references or "." not in column.references:
                continue

            parent_name, parent_column = column.references.split(".", 1)
            parent = by_name.get(parent_name)
            if parent is None:
                continue

            if engine_name == "mongodb":
                documents.append(
                    _mongo_join_example(table, column.name, parent_name, parent_column)
                )
            else:
                # Prefer the parent's actual declared PK if introspection
                # found one; fall back to the column the FK explicitly
                # references (e.g. a unique-but-not-primary key) either way.
                parent_pk = _find_pk_column(parent) or parent_column
                documents.append(
                    _sql_join_example(table, column.name, parent_name, parent_pk, engine_name)
                )

    return documents


def seed_fk_join_examples(
    user_id: int,
    connection_id: int,
    engine_name: str,
    tables: Sequence[TableSchema],
    client: Optional[QdrantClient] = None,
) -> int:
    """Generate and index the FK-derived examples for one connection.
    Best-effort: any failure here must not fail connection registration,
    which is why app/api/connections.py wraps this call in a try/except
    and treats it as optional polish, not a required step (unlike schema
    indexing itself)."""
    documents = build_fk_join_documents(tables, engine_name)
    if not documents:
        return 0

    client = client or _client()
    ensure_collection(client)

    questions = [document.question for document in documents]
    embeddings = embed_texts(questions)
    if not embeddings or len(embeddings) != len(documents):
        return 0

    points = [
        qmodels.PointStruct(
            id=_point_id(user_id, connection_id, document.question),
            vector=list(embedding),
            payload={
                "user_id": user_id,
                "connection_id": connection_id,
                "engine": engine_name,
                "question": document.question,
                "query": document.query,
                "seeded": True,
            },
        )
        for document, embedding in zip(documents, embeddings)
    ]
    client.upsert(collection_name=COLLECTION_NAME, points=points)
    return len(points)
