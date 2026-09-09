"""
Tests for app/engine/schema_rag.py's search isolation boundary - the single
most important property in this project.

These run against a REAL Qdrant instance (the same one docker-compose
starts - QDRANT_URL, defaulting to the docker-compose service name "qdrant"
so this also works when the suite runs inside the backend container;
override with QDRANT_TEST_URL when running from the host, e.g.
QDRANT_TEST_URL=http://localhost:6333), using a disposable, uniquely-named
collection created and dropped per test - never the real
`private_data_assistant_schema` collection.

Every test upserts small fake points (short synthetic vectors, not real
Azure embeddings - the payload-filter behavior under test doesn't depend on
vector content at all) through `upsert_table_points`, using its `client=`
override to target the disposable collection.

What is being proved, and why it's stricter than the sibling project's
document isolation: schema search filters on `user_id` AND `connection_id`,
both unconditionally. There is no "search across all my connections" mode,
because writing a query against connection A using connection B's table
names is never useful and potentially reads the wrong system. And because
`connection_id` is a per-user-shaped integer, two different users routinely
hold the SAME numeric connection_id - so neither filter can carry the
weight alone.
"""

import os
import uuid

import pytest
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from app.engine import schema_rag
from app.engine.db_adapters.base import ColumnSchema, TableSchema
from app.engine.schema_rag import TableDocument

VECTOR_SIZE = 8


def _vec(seed: int) -> list:
    """A small deterministic "embedding" - distinct per seed, but the
    isolation filter under test cares only about payload matching, so any
    fixed-length vector works."""
    return [float((seed + i) % 7) / 7.0 for i in range(VECTOR_SIZE)]


def _write(client, user_id, connection_id, table_name, seed, engine="postgres"):
    return schema_rag.upsert_table_points(
        user_id=user_id,
        connection_id=connection_id,
        engine_name=engine,
        documents=[TableDocument(table_name=table_name, text=f"Table: {table_name}")],
        embeddings=[_vec(seed)],
        client=client,
    )


@pytest.fixture()
def qdrant_url() -> str:
    return os.getenv("QDRANT_TEST_URL") or os.getenv("QDRANT_URL") or "http://qdrant:6333"


@pytest.fixture()
def isolated_collection(qdrant_url, monkeypatch):
    """Create a uniquely-named Qdrant collection for one test, point
    schema_rag.COLLECTION_NAME at it (so upsert_table_points()'s internal
    ensure_collection() call is a no-op against a collection that already
    exists with our test vector size), and drop it afterwards."""
    client = QdrantClient(url=qdrant_url)
    collection_name = f"test_schema_isolation_{uuid.uuid4().hex[:10]}"

    client.create_collection(
        collection_name=collection_name,
        vectors_config=qmodels.VectorParams(
            size=VECTOR_SIZE, distance=qmodels.Distance.COSINE
        ),
    )
    monkeypatch.setattr(schema_rag, "COLLECTION_NAME", collection_name)

    try:
        yield client
    finally:
        client.delete_collection(collection_name)


# --- (a) user_id mismatch returns nothing, even when connection_id matches --


def test_wrong_user_id_returns_nothing(isolated_collection):
    client = isolated_collection
    _write(client, user_id=1, connection_id=100, table_name="orders", seed=1)

    results = schema_rag.search_schema(
        _vec(1), user_id=999, connection_id=100, top_k=10, client=client
    )

    assert results == []


def test_wrong_user_id_returns_nothing_even_when_connection_id_matches(isolated_collection):
    """The crux of the guarantee: guessing the right connection_id must not
    be enough. connection_id alone is never sufficient."""
    client = isolated_collection
    _write(client, user_id=1, connection_id=100, table_name="salaries", seed=1)

    results = schema_rag.search_schema(
        _vec(1), user_id=2, connection_id=100, top_k=10, client=client
    )

    assert results == []


def test_two_users_reusing_the_same_connection_id_stay_isolated(isolated_collection):
    """connection_id is just an integer, and two users routinely have
    connections with the same numeric id. The user_id filter must still keep
    their schemas apart - in BOTH directions."""
    client = isolated_collection
    _write(client, user_id=1, connection_id=5, table_name="user1_orders", seed=1)
    _write(client, user_id=2, connection_id=5, table_name="user2_orders", seed=2)

    user1 = schema_rag.search_schema(
        _vec(1), user_id=1, connection_id=5, top_k=10, client=client
    )
    user2 = schema_rag.search_schema(
        _vec(2), user_id=2, connection_id=5, top_k=10, client=client
    )

    assert [r.table_name for r in user1] == ["user1_orders"]
    assert [r.table_name for r in user2] == ["user2_orders"]

    # Even querying with the OTHER user's exact vector (the closest possible
    # match to their point) returns only your own - similarity never
    # overrides the filter.
    cross = schema_rag.search_schema(
        _vec(2), user_id=1, connection_id=5, top_k=10, client=client
    )
    assert [r.table_name for r in cross] == ["user1_orders"]


def test_two_users_with_identically_named_tables_do_not_overwrite_each_other(
    isolated_collection,
):
    """Point ids are derived from (user, connection, table) - two users
    whose connection 5 both have an `orders` table must produce two
    distinct points, not one that clobbers the other."""
    client = isolated_collection
    _write(client, user_id=1, connection_id=5, table_name="orders", seed=1)
    _write(client, user_id=2, connection_id=5, table_name="orders", seed=2)

    assert schema_rag.schema_stats(1, 5, client=client)["indexed_tables"] == 1
    assert schema_rag.schema_stats(2, 5, client=client)["indexed_tables"] == 1


# --- (b) connection_id mismatch returns nothing, even when user_id matches --


def test_wrong_connection_id_returns_nothing_even_for_the_right_user(isolated_collection):
    client = isolated_collection
    _write(client, user_id=1, connection_id=10, table_name="orders", seed=1)

    results = schema_rag.search_schema(
        _vec(1), user_id=1, connection_id=20, top_k=10, client=client
    )

    assert results == []


def test_one_users_two_connections_never_bleed_into_each_other(isolated_collection):
    """A user with a production Postgres and a staging MySQL must never get
    staging's tables offered up when asking about production - writing a
    query against the wrong system is exactly the failure this prevents."""
    client = isolated_collection
    _write(client, 1, 10, "prod_orders", seed=1, engine="postgres")
    _write(client, 1, 20, "staging_orders", seed=2, engine="mysql")

    prod = schema_rag.search_schema(
        _vec(2), user_id=1, connection_id=10, top_k=10, client=client
    )
    staging = schema_rag.search_schema(
        _vec(1), user_id=1, connection_id=20, top_k=10, client=client
    )

    # Queried with the OTHER connection's own vector in both directions -
    # the connection_id filter, not similarity, decides eligibility.
    assert [r.table_name for r in prod] == ["prod_orders"]
    assert [r.table_name for r in staging] == ["staging_orders"]


# --- (c) the happy path, and the payload the retrieval context relies on ----


def test_search_returns_every_table_of_the_matching_connection(isolated_collection):
    client = isolated_collection
    _write(client, 1, 10, "orders", seed=1)
    _write(client, 1, 10, "customers", seed=2)
    _write(client, 1, 10, "products", seed=3)

    results = schema_rag.search_schema(
        _vec(1), user_id=1, connection_id=10, top_k=10, client=client
    )

    assert {r.table_name for r in results} == {"orders", "customers", "products"}


def test_search_result_carries_the_engine_so_the_prompt_knows_the_dialect(
    isolated_collection,
):
    client = isolated_collection
    _write(client, 1, 10, "orders", seed=1, engine="mssql")

    [result] = schema_rag.search_schema(
        _vec(1), user_id=1, connection_id=10, top_k=10, client=client
    )

    assert result.engine == "mssql"
    assert result.text == "Table: orders"


def test_top_k_limits_how_many_tables_come_back(isolated_collection):
    client = isolated_collection
    for index in range(6):
        _write(client, 1, 10, f"table_{index}", seed=index)

    results = schema_rag.search_schema(
        _vec(0), user_id=1, connection_id=10, top_k=3, client=client
    )

    assert len(results) == 3


# --- (d) deletion is scoped the same way ------------------------------------


def test_delete_connection_schema_removes_only_that_connections_points(
    isolated_collection,
):
    client = isolated_collection
    _write(client, 1, 10, "keep_me", seed=1)
    _write(client, 1, 20, "delete_me", seed=2)

    schema_rag.delete_connection_schema(1, 20, client=client)

    assert schema_rag.schema_stats(1, 10, client=client)["indexed_tables"] == 1
    assert schema_rag.schema_stats(1, 20, client=client)["indexed_tables"] == 0


def test_delete_connection_schema_never_touches_another_users_points(isolated_collection):
    """Deletion filters on user_id too, so a bug that passed the wrong
    connection_id still can't reach into another account."""
    client = isolated_collection
    _write(client, 1, 5, "user1_orders", seed=1)
    _write(client, 2, 5, "user2_orders", seed=2)

    schema_rag.delete_connection_schema(1, 5, client=client)

    assert schema_rag.schema_stats(1, 5, client=client)["indexed_tables"] == 0
    assert schema_rag.schema_stats(2, 5, client=client)["indexed_tables"] == 1


def test_reindexing_the_same_connection_overwrites_rather_than_duplicates(
    isolated_collection,
):
    client = isolated_collection
    _write(client, 1, 10, "orders", seed=1)
    _write(client, 1, 10, "orders", seed=2)  # same table, re-indexed

    assert schema_rag.schema_stats(1, 10, client=client)["indexed_tables"] == 1


# --- chunk building (pure, no Qdrant needed) --------------------------------


def test_build_table_documents_renders_columns_keys_and_sample_rows():
    tables = [
        TableSchema(
            name="orders",
            columns=[
                ColumnSchema(name="id", type="INTEGER", nullable=False, is_pk=True),
                ColumnSchema(
                    name="customer_id",
                    type="INTEGER",
                    nullable=True,
                    is_fk=True,
                    references="customers.id",
                ),
                ColumnSchema(name="status", type="VARCHAR", nullable=False),
            ],
            sample_rows=[{"id": 1, "customer_id": 7, "status": "shipped"}],
        )
    ]

    [document] = schema_rag.build_table_documents(tables)

    assert document.table_name == "orders"
    assert "Table: orders" in document.text
    assert "primary key" in document.text
    assert "foreign key -> customers.id" in document.text
    assert "not null" in document.text
    # The sample row is rendered as a markdown table, values included -
    # that's what shows the model the real format of each column.
    assert "| id | customer_id | status |" in document.text
    assert "shipped" in document.text


def test_build_table_documents_handles_a_table_with_no_columns_or_rows():
    [document] = schema_rag.build_table_documents([TableSchema(name="empty")])
    assert "Table: empty" in document.text
    assert "no column information" in document.text


def test_build_table_documents_truncates_an_enormous_table():
    columns = [
        ColumnSchema(name=f"column_{index}", type="TEXT", nullable=True)
        for index in range(2000)
    ]
    [document] = schema_rag.build_table_documents(
        [TableSchema(name="wide", columns=columns)]
    )
    assert len(document.text) <= schema_rag.MAX_CHUNK_CHARS + 40
    assert document.text.endswith("(schema truncated)")
