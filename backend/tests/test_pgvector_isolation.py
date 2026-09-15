"""
Tests for app/engine/schema_rag.py's search isolation boundary - the single
most important property in this project.

These run against a REAL Postgres instance with the `vector` extension
available (the same database docker-compose's `postgres` service - now the
`pgvector/pgvector` image - provides; override with TEST_DATABASE_URL to
point at a different one, e.g. a Neon/Vercel Postgres branch). Each test
gets its own disposable, uniquely-named Postgres SCHEMA (never the
project's real `public` schema), with its own copies of the
`schema_chunks`/`example_chunks` tables created fresh and dropped after -
the pgvector equivalent of the old per-test disposable Qdrant collection.

If no such database is reachable (no Postgres running, `vector` extension
unavailable, insufficient privileges to CREATE SCHEMA/EXTENSION), every
test in this file is SKIPPED rather than erroring the whole suite - see
`pgvector_engine` below. This is the one thing in this project that
genuinely cannot be verified without a live Postgres+pgvector instance;
note that in any report on this test run.

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
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.engine import schema_rag, vector_store
from app.engine.azure_client import get_embedding_dimensions
from app.engine.db_adapters.base import ColumnSchema, TableSchema
from app.engine.schema_rag import TableDocument

VECTOR_SIZE = get_embedding_dimensions()


def _vec(seed: int) -> list:
    """A small deterministic "embedding" - distinct per seed, but the
    isolation filter under test cares only about payload matching, so any
    fixed-length vector works. Sized to match the real embedding
    dimension, since that's baked into the pgvector column type."""
    return [float((seed + i) % 7) / 7.0 for i in range(VECTOR_SIZE)]


def _write(engine, user_id, connection_id, table_name, seed, db_engine="postgres"):
    return schema_rag.upsert_table_points(
        user_id=user_id,
        connection_id=connection_id,
        engine_name=db_engine,
        documents=[TableDocument(table_name=table_name, text=f"Table: {table_name}")],
        embeddings=[_vec(seed)],
        client=engine,
    )


@pytest.fixture()
def pgvector_engine():
    """A fresh, uniquely-named Postgres SCHEMA with its own schema_chunks/
    example_chunks tables, targeted by setting `search_path` on every
    connection this engine opens (so vector_store's unqualified Table
    objects resolve into it, not the real `public` schema) - dropped again
    afterwards. Skips the whole test if no usable Postgres+pgvector
    instance is reachable."""
    url = (
        os.getenv("TEST_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or "postgresql://postgres:postgres@localhost:5432/private_data_assistant"
    )
    schema_name = f"test_pgvector_{uuid.uuid4().hex[:10]}"

    try:
        setup_engine = create_engine(url)
        with setup_engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"No usable Postgres+pgvector instance reachable: {exc}")
        return

    engine: Engine = create_engine(
        url, connect_args={"options": f"-csearch_path={schema_name},public"}
    )
    try:
        # checkfirst=False is required, not just belt-and-braces: Table
        # objects here have no explicit `schema=`, so SQLAlchemy's default
        # existence check (checkfirst=True) asks Postgres for an
        # UNQUALIFIED "schema_chunks" - which search_path resolves against
        # EVERY schema on the path, `public` included. If the real
        # deployment's own Alembic migration has already run against this
        # same database (the normal case - TEST_DATABASE_URL usually points
        # at the same Postgres docker-compose/dev already migrated),
        # `public.schema_chunks` already exists, `has_table()` reports true,
        # and create_all silently skips creating a fresh copy in THIS
        # test's schema - so every unqualified insert/select then falls
        # through search_path to the real `public` table instead of this
        # disposable one, silently breaking isolation between test runs.
        vector_store.metadata.create_all(bind=engine, checkfirst=False)
    except Exception as exc:  # noqa: BLE001
        engine.dispose()
        with setup_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
        setup_engine.dispose()
        pytest.skip(f"Could not create pgvector tables for this test: {exc}")
        return

    try:
        yield engine
    finally:
        engine.dispose()
        with setup_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
        setup_engine.dispose()


# --- (a) user_id mismatch returns nothing, even when connection_id matches --


def test_wrong_user_id_returns_nothing(pgvector_engine):
    engine = pgvector_engine
    _write(engine, user_id=1, connection_id=100, table_name="orders", seed=1)

    results = schema_rag.search_schema(
        _vec(1), user_id=999, connection_id=100, top_k=10, client=engine
    )

    assert results == []


def test_wrong_user_id_returns_nothing_even_when_connection_id_matches(pgvector_engine):
    """The crux of the guarantee: guessing the right connection_id must not
    be enough. connection_id alone is never sufficient."""
    engine = pgvector_engine
    _write(engine, user_id=1, connection_id=100, table_name="salaries", seed=1)

    results = schema_rag.search_schema(
        _vec(1), user_id=2, connection_id=100, top_k=10, client=engine
    )

    assert results == []


def test_two_users_reusing_the_same_connection_id_stay_isolated(pgvector_engine):
    """connection_id is just an integer, and two users routinely have
    connections with the same numeric id. The user_id filter must still keep
    their schemas apart - in BOTH directions."""
    engine = pgvector_engine
    _write(engine, user_id=1, connection_id=5, table_name="user1_orders", seed=1)
    _write(engine, user_id=2, connection_id=5, table_name="user2_orders", seed=2)

    user1 = schema_rag.search_schema(
        _vec(1), user_id=1, connection_id=5, top_k=10, client=engine
    )
    user2 = schema_rag.search_schema(
        _vec(2), user_id=2, connection_id=5, top_k=10, client=engine
    )

    assert [r.table_name for r in user1] == ["user1_orders"]
    assert [r.table_name for r in user2] == ["user2_orders"]

    # Even querying with the OTHER user's exact vector (the closest possible
    # match to their point) returns only your own - similarity never
    # overrides the filter.
    cross = schema_rag.search_schema(
        _vec(2), user_id=1, connection_id=5, top_k=10, client=engine
    )
    assert [r.table_name for r in cross] == ["user1_orders"]


def test_two_users_with_identically_named_tables_do_not_overwrite_each_other(
    pgvector_engine,
):
    """Row ids are derived from (user, connection, table) - two users
    whose connection 5 both have an `orders` table must produce two
    distinct rows, not one that clobbers the other."""
    engine = pgvector_engine
    _write(engine, user_id=1, connection_id=5, table_name="orders", seed=1)
    _write(engine, user_id=2, connection_id=5, table_name="orders", seed=2)

    assert schema_rag.schema_stats(1, 5, client=engine)["indexed_tables"] == 1
    assert schema_rag.schema_stats(2, 5, client=engine)["indexed_tables"] == 1


# --- (b) connection_id mismatch returns nothing, even when user_id matches --


def test_wrong_connection_id_returns_nothing_even_for_the_right_user(pgvector_engine):
    engine = pgvector_engine
    _write(engine, user_id=1, connection_id=10, table_name="orders", seed=1)

    results = schema_rag.search_schema(
        _vec(1), user_id=1, connection_id=20, top_k=10, client=engine
    )

    assert results == []


def test_one_users_two_connections_never_bleed_into_each_other(pgvector_engine):
    """A user with a production Postgres and a staging MySQL must never get
    staging's tables offered up when asking about production - writing a
    query against the wrong system is exactly the failure this prevents."""
    engine = pgvector_engine
    _write(engine, 1, 10, "prod_orders", seed=1, db_engine="postgres")
    _write(engine, 1, 20, "staging_orders", seed=2, db_engine="mysql")

    prod = schema_rag.search_schema(
        _vec(2), user_id=1, connection_id=10, top_k=10, client=engine
    )
    staging = schema_rag.search_schema(
        _vec(1), user_id=1, connection_id=20, top_k=10, client=engine
    )

    # Queried with the OTHER connection's own vector in both directions -
    # the connection_id filter, not similarity, decides eligibility.
    assert [r.table_name for r in prod] == ["prod_orders"]
    assert [r.table_name for r in staging] == ["staging_orders"]


# --- (c) the happy path, and the payload the retrieval context relies on ----


def test_search_returns_every_table_of_the_matching_connection(pgvector_engine):
    engine = pgvector_engine
    _write(engine, 1, 10, "orders", seed=1)
    _write(engine, 1, 10, "customers", seed=2)
    _write(engine, 1, 10, "products", seed=3)

    results = schema_rag.search_schema(
        _vec(1), user_id=1, connection_id=10, top_k=10, client=engine
    )

    assert {r.table_name for r in results} == {"orders", "customers", "products"}


def test_search_result_carries_the_engine_so_the_prompt_knows_the_dialect(
    pgvector_engine,
):
    engine = pgvector_engine
    _write(engine, 1, 10, "orders", seed=1, db_engine="mssql")

    [result] = schema_rag.search_schema(
        _vec(1), user_id=1, connection_id=10, top_k=10, client=engine
    )

    assert result.engine == "mssql"
    assert result.text == "Table: orders"


def test_top_k_limits_how_many_tables_come_back(pgvector_engine):
    engine = pgvector_engine
    for index in range(6):
        _write(engine, 1, 10, f"table_{index}", seed=index)

    results = schema_rag.search_schema(
        _vec(0), user_id=1, connection_id=10, top_k=3, client=engine
    )

    assert len(results) == 3


# --- (d) deletion is scoped the same way ------------------------------------


def test_delete_connection_schema_removes_only_that_connections_points(
    pgvector_engine,
):
    engine = pgvector_engine
    _write(engine, 1, 10, "keep_me", seed=1)
    _write(engine, 1, 20, "delete_me", seed=2)

    schema_rag.delete_connection_schema(1, 20, client=engine)

    assert schema_rag.schema_stats(1, 10, client=engine)["indexed_tables"] == 1
    assert schema_rag.schema_stats(1, 20, client=engine)["indexed_tables"] == 0


def test_delete_connection_schema_never_touches_another_users_points(pgvector_engine):
    """Deletion filters on user_id too, so a bug that passed the wrong
    connection_id still can't reach into another account."""
    engine = pgvector_engine
    _write(engine, 1, 5, "user1_orders", seed=1)
    _write(engine, 2, 5, "user2_orders", seed=2)

    schema_rag.delete_connection_schema(1, 5, client=engine)

    assert schema_rag.schema_stats(1, 5, client=engine)["indexed_tables"] == 0
    assert schema_rag.schema_stats(2, 5, client=engine)["indexed_tables"] == 1


def test_reindexing_the_same_connection_overwrites_rather_than_duplicates(
    pgvector_engine,
):
    engine = pgvector_engine
    _write(engine, 1, 10, "orders", seed=1)
    _write(engine, 1, 10, "orders", seed=2)  # same table, re-indexed

    assert schema_rag.schema_stats(1, 10, client=engine)["indexed_tables"] == 1


# --- chunk building (pure, no database needed) ------------------------------


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
