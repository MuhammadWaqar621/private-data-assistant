"""
Tests for app/engine/db_adapters/readonly.py - the read-only guard.

This is the security core of the project: an LLM writes the queries, so
"the prompt says SELECT only" is a policy, not a control. Everything here
is tested as PURE FUNCTIONS (readonly.py imports nothing but the standard
library), which means no MySQL, SQL Server or MongoDB server is needed
anywhere in this file - and the MongoDB guard in particular is exercised
directly rather than through a driver.

The one place a real database is used is a stdlib in-memory SQLite
database, to prove end-to-end that the SQLite adapter actually applies its
cap and actually refuses to write.
"""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.engine.db_adapters.base import ConnectionInfo
from app.engine.db_adapters.errors import NotReadOnlyError, QueryExecutionError
from app.engine.db_adapters.readonly import (
    BLOCKED_KEYWORDS,
    assert_read_only_sql,
    strip_sql_comments,
    validate_mongo_operation,
    wrap_with_row_limit,
)
from app.engine.db_adapters.sqlite_adapter import SQLiteAdapter

# --- (1) SELECT / WITH are allowed -----------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select id, name from users",
        "  SELECT * FROM orders WHERE total > 10  ",
        "WITH recent AS (SELECT * FROM orders) SELECT count(*) FROM recent",
        "with recent as (select 1) select * from recent",
        "SELECT 1;",  # a single trailing semicolon is fine
        "/* a leading comment */ SELECT 1",
        "-- a leading line comment\nSELECT 1",
    ],
)
def test_select_and_with_pass_the_guard(sql):
    cleaned = assert_read_only_sql(sql)
    assert cleaned  # returns the executable text
    assert not cleaned.endswith(";")


def test_guard_returns_the_comment_stripped_text_to_execute():
    cleaned = assert_read_only_sql("/* pick users */ SELECT id FROM users -- trailing")
    assert "pick users" not in cleaned
    assert "trailing" not in cleaned
    assert cleaned.strip().upper().startswith("SELECT ID FROM USERS")


def test_strip_sql_comments_leaves_string_contents_alone():
    """A `--` inside a string literal is data, not a comment."""
    sql = "SELECT 'a -- b' AS note"
    assert "a -- b" in strip_sql_comments(sql)


# --- (2) non-SELECT statements are rejected outright ------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO users (email) VALUES ('x')",
        "UPDATE users SET email = 'x'",
        "DELETE FROM users",
        "DROP TABLE users",
        "ALTER TABLE users ADD COLUMN x int",
        "CREATE TABLE t (id int)",
        "TRUNCATE TABLE users",
        "GRANT ALL ON users TO public",
        "REVOKE ALL ON users FROM public",
        "EXEC sp_who",
        "EXECUTE sp_who",
        "CALL do_something()",
        "MERGE INTO t USING s ON (1=1) WHEN MATCHED THEN UPDATE SET a = 1",
        "REPLACE INTO t VALUES (1)",
        "ATTACH DATABASE 'other.db' AS other",
        "DETACH DATABASE other",
        "PRAGMA journal_mode = WAL",
    ],
)
def test_every_blocklisted_statement_is_rejected(sql):
    with pytest.raises(NotReadOnlyError):
        assert_read_only_sql(sql)


@pytest.mark.parametrize("keyword", BLOCKED_KEYWORDS)
def test_every_blocklisted_keyword_is_rejected_even_inside_a_select(keyword):
    """Not just as the opening word: a blocklisted keyword anywhere in the
    statement (a smuggled subquery, a CTE body) is refused."""
    with pytest.raises(NotReadOnlyError) as exc:
        assert_read_only_sql(f"SELECT * FROM t WHERE x = 1 {keyword} y")
    assert keyword in str(exc.value).upper()


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "-- just a comment",
        "/* just a block comment */",
        "SET search_path = public",
        "BEGIN; SELECT 1",
        "VALUES (1)",
        "SHOW TABLES",
    ],
)
def test_anything_that_isnt_a_select_or_with_is_rejected(sql):
    with pytest.raises(NotReadOnlyError):
        assert_read_only_sql(sql)


# --- word-boundary matching: no false positives -----------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT updated_at FROM orders",
        "SELECT created_at, deleted_flag FROM orders",
        "SELECT o.update_count FROM orders o",
        "SELECT insertion_point FROM shapes",
        "SELECT calls_total FROM metrics",
        "SELECT dropoff_rate FROM funnel",
    ],
)
def test_columns_that_merely_contain_a_keyword_are_not_blocked(sql):
    """`updated_at` contains "update" but is not the UPDATE keyword - the
    guard uses word-boundary matching precisely so ordinary column names
    keep working."""
    assert assert_read_only_sql(sql)


def test_a_keyword_inside_a_string_literal_is_not_blocked():
    """String literals are masked before the blocklist runs, so data that
    happens to contain a keyword doesn't get the query refused."""
    assert assert_read_only_sql(
        "SELECT * FROM notes WHERE body = 'please delete everything'"
    )


def test_a_quoted_identifier_named_like_a_keyword_is_not_blocked():
    assert assert_read_only_sql('SELECT "update" FROM audit')


# --- (3) exactly one statement ----------------------------------------------


def test_multiple_statements_are_rejected():
    with pytest.raises(NotReadOnlyError) as exc:
        assert_read_only_sql("SELECT 1; SELECT 2")
    assert "one statement" in str(exc.value)


def test_a_write_smuggled_after_a_semicolon_is_rejected():
    with pytest.raises(NotReadOnlyError):
        assert_read_only_sql("SELECT 1; DROP TABLE users")


def test_a_semicolon_inside_a_string_is_not_a_statement_separator():
    assert assert_read_only_sql("SELECT * FROM t WHERE s = 'a;b'")


def test_a_write_hidden_behind_a_comment_is_still_rejected():
    """Comments are stripped BEFORE the checks, so commenting out the
    separator doesn't help."""
    with pytest.raises(NotReadOnlyError):
        assert_read_only_sql("SELECT 1 /* nothing to see */ ; DELETE FROM users")


# --- (5) row-cap wrapping is dialect-correct ---------------------------------


@pytest.mark.parametrize("engine", ["postgres", "mysql", "sqlite"])
def test_limit_engines_are_wrapped_with_a_trailing_limit(engine):
    wrapped = wrap_with_row_limit("SELECT a FROM t", 25, engine)
    assert wrapped is not None
    assert wrapped.rstrip().endswith("LIMIT 25")
    assert "AS _sub" in wrapped
    assert "TOP" not in wrapped.upper()


def test_mssql_is_wrapped_with_top_because_it_has_no_limit_clause():
    wrapped = wrap_with_row_limit("SELECT a FROM t", 25, "mssql")
    assert wrapped is not None
    assert wrapped.upper().startswith("SELECT TOP (25) *")
    assert "LIMIT" not in wrapped.upper()


def test_mssql_refuses_to_wrap_a_cte_because_t_sql_forbids_with_in_a_derived_table():
    assert wrap_with_row_limit("WITH c AS (SELECT 1) SELECT * FROM c", 25, "mssql") is None


def test_mssql_refuses_to_wrap_an_order_by_because_t_sql_forbids_it_in_a_derived_table():
    assert wrap_with_row_limit("SELECT a FROM t ORDER BY a", 25, "mssql") is None


def test_other_engines_happily_wrap_a_cte_and_an_order_by():
    assert wrap_with_row_limit("WITH c AS (SELECT 1) SELECT * FROM c", 25, "postgres")
    assert wrap_with_row_limit("SELECT a FROM t ORDER BY a", 25, "sqlite")


def test_unknown_engines_are_not_wrapped_at_all():
    assert wrap_with_row_limit("SELECT 1", 25, "mongodb") is None


def test_row_cap_is_always_at_least_one():
    assert "LIMIT 1" in wrap_with_row_limit("SELECT 1", 0, "postgres")


# --- MongoDB: the guard as a pure function -----------------------------------


def test_mongo_find_is_normalized_and_capped():
    spec = validate_mongo_operation(
        json.dumps(
            {
                "operation": "find",
                "collection": "orders",
                "filter": {"status": "shipped"},
                "limit": 100000,
            }
        ),
        max_rows=200,
    )
    assert spec["operation"] == "find"
    assert spec["collection"] == "orders"
    assert spec["limit"] == 200  # the model's oversized limit is clamped


def test_mongo_find_respects_a_smaller_model_supplied_limit():
    spec = validate_mongo_operation(
        json.dumps({"operation": "find", "collection": "o", "limit": 5}), max_rows=200
    )
    assert spec["limit"] == 5


def test_mongo_aggregate_gets_a_limit_stage_appended():
    spec = validate_mongo_operation(
        json.dumps(
            {
                "operation": "aggregate",
                "collection": "orders",
                "pipeline": [{"$group": {"_id": "$status"}}],
            }
        ),
        max_rows=50,
    )
    assert spec["pipeline"][-1] == {"$limit": 50}


def test_mongo_aggregate_leaves_an_existing_smaller_final_limit_alone():
    spec = validate_mongo_operation(
        json.dumps(
            {
                "operation": "aggregate",
                "collection": "orders",
                "pipeline": [{"$match": {}}, {"$limit": 10}],
            }
        ),
        max_rows=50,
    )
    assert spec["pipeline"] == [{"$match": {}}, {"$limit": 10}]


@pytest.mark.parametrize(
    "operation", ["insertOne", "updateMany", "deleteOne", "drop", "mapReduce", "eval"]
)
def test_mongo_write_operations_are_rejected(operation):
    with pytest.raises(NotReadOnlyError):
        validate_mongo_operation(
            json.dumps({"operation": operation, "collection": "o"}), max_rows=10
        )


@pytest.mark.parametrize(
    "stage",
    [
        {"$out": "copies"},
        {"$merge": {"into": "copies"}},
        {"$function": {"body": "function(){}"}},
        {"$accumulator": {}},
        {"$where": "this.a == 1"},
    ],
)
def test_mongo_blocklisted_pipeline_stages_are_rejected(stage):
    with pytest.raises(NotReadOnlyError):
        validate_mongo_operation(
            json.dumps({"operation": "aggregate", "collection": "o", "pipeline": [stage]}),
            max_rows=10,
        )


def test_mongo_where_is_rejected_inside_a_find_filter_too():
    """$where runs server-side JavaScript, so it's just as dangerous in a
    plain filter as in a pipeline stage - the blocklist is applied
    recursively, not only to top-level stage keys."""
    with pytest.raises(NotReadOnlyError):
        validate_mongo_operation(
            json.dumps(
                {"operation": "find", "collection": "o", "filter": {"$where": "1 == 1"}}
            ),
            max_rows=10,
        )


def test_mongo_nested_blocklisted_operator_is_rejected():
    with pytest.raises(NotReadOnlyError):
        validate_mongo_operation(
            json.dumps(
                {
                    "operation": "aggregate",
                    "collection": "o",
                    "pipeline": [{"$match": {"a": {"$where": "x"}}}],
                }
            ),
            max_rows=10,
        )


def test_mongo_malformed_json_is_rejected():
    with pytest.raises(QueryExecutionError):
        validate_mongo_operation("SELECT * FROM orders", max_rows=10)


def test_mongo_missing_collection_is_rejected():
    with pytest.raises(QueryExecutionError):
        validate_mongo_operation(json.dumps({"operation": "find"}), max_rows=10)


def test_mongo_count_and_distinct_are_allowed():
    count = validate_mongo_operation(
        json.dumps({"operation": "count", "collection": "o"}), max_rows=10
    )
    assert count["operation"] == "count"

    distinct = validate_mongo_operation(
        json.dumps({"operation": "distinct", "collection": "o", "field": "status"}),
        max_rows=10,
    )
    assert distinct["field"] == "status"


# --- End-to-end against a real (stdlib) SQLite database ---------------------


@pytest.fixture()
def sqlite_connection():
    """A throwaway SQLite file with a few rows, wired up as a
    ConnectionInfo exactly like a real uploaded database would be."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "database.sqlite"
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT NOT NULL, total REAL)"
        )
        connection.executemany(
            "INSERT INTO orders (status, total) VALUES (?, ?)",
            [("shipped", 10.0), ("shipped", 20.0), ("pending", 5.0), ("cancelled", 1.0)],
        )
        connection.commit()
        connection.close()

        yield ConnectionInfo(
            engine="sqlite",
            database="database.sqlite",
            extra_params={"storage_path": str(path)},
        )


def test_sqlite_adapter_reads_rows(sqlite_connection):
    columns, rows = SQLiteAdapter().execute_read_only(
        sqlite_connection, "SELECT status, total FROM orders", max_rows=100, timeout_seconds=5
    )
    assert columns == ["status", "total"]
    assert len(rows) == 4


def test_sqlite_adapter_enforces_the_row_cap(sqlite_connection):
    _, rows = SQLiteAdapter().execute_read_only(
        sqlite_connection, "SELECT * FROM orders", max_rows=2, timeout_seconds=5
    )
    assert len(rows) == 2


def test_sqlite_adapter_rejects_a_write_before_touching_the_database(sqlite_connection):
    with pytest.raises(NotReadOnlyError):
        SQLiteAdapter().execute_read_only(
            sqlite_connection,
            "DELETE FROM orders",
            max_rows=10,
            timeout_seconds=5,
        )


def test_sqlite_file_is_opened_read_only_so_a_write_fails_even_at_the_driver(
    sqlite_connection,
):
    """Belt and braces: bypass the guard entirely and go straight at the
    adapter's connection. `mode=ro` means SQLite itself refuses the write,
    so the guard is not the only thing standing between a model and the
    user's data."""
    adapter = SQLiteAdapter()
    connection = adapter._connect(sqlite_connection, timeout_seconds=5)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM orders")
    finally:
        connection.close()


def test_sqlite_adapter_introspects_columns_keys_and_samples(sqlite_connection):
    [table] = SQLiteAdapter().introspect_schema(sqlite_connection)
    assert table.name == "orders"
    by_name = {column.name: column for column in table.columns}
    assert by_name["id"].is_pk is True
    assert by_name["status"].nullable is False
    assert len(table.sample_rows) == 4  # fewer rows than the sample limit


def test_sqlite_adapter_test_connection_reports_a_missing_file_instead_of_raising():
    ok, error = SQLiteAdapter().test_connection(
        ConnectionInfo(
            engine="sqlite",
            database="nope.sqlite",
            extra_params={"storage_path": "/definitely/not/here.sqlite"},
        )
    )
    assert ok is False
    assert error and "missing" in error.lower()


def test_sqlite_adapter_surfaces_a_bad_query_as_a_query_execution_error(sqlite_connection):
    with pytest.raises(QueryExecutionError):
        SQLiteAdapter().execute_read_only(
            sqlite_connection,
            "SELECT no_such_column FROM orders",
            max_rows=10,
            timeout_seconds=5,
        )
