"""
Shared implementation for the SQLAlchemy-backed adapters (postgres, mysql,
mssql).

SQLite is deliberately NOT built on this: it has no host/port/credentials,
its "server" is a file this app stores itself, and the stdlib `sqlite3`
module gives us a genuine OS-level read-only open (`mode=ro`) plus an
interrupt-based timeout that SQLAlchemy would only get in the way of - see
sqlite_adapter.py.

What lives here (identical for all three engines):

  * engine construction from a ConnectionInfo, with connect/query timeouts
  * `test_connection` (connect + `SELECT 1`, never raises)
  * `introspect_schema` via SQLAlchemy's `Inspector` (columns, types,
    nullability, primary keys, foreign keys) plus a small sample of real
    rows per table
  * `execute_read_only`: static guard -> engine-correct row-cap wrapping
    (with an unwrapped + Python-truncated fallback) -> execution inside a
    transaction that is ALWAYS rolled back
  * translation of every driver exception into QueryExecutionError /
    ConnectionFailedError

What subclasses supply:

  * `engine_name` and `_build_url()`
  * `_connect_args()` (driver-specific TLS/timeout knobs)
  * `_pre_begin_statements()` - best-effort session settings, failures
    ignored (a MariaDB server rejecting MySQL's MAX_EXECUTION_TIME must not
    break the query)
  * `_in_transaction_statements()` - settings that must hold for the
    query itself (Postgres' `SET TRANSACTION READ ONLY`); failures here are
    NOT swallowed
  * `_sample_rows_sql()` - `LIMIT n` vs `TOP (n)`
"""

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from app.engine.db_adapters.base import (
    SAMPLE_ROW_LIMIT,
    ColumnSchema,
    ConnectionInfo,
    DBAdapter,
    TableSchema,
    json_safe,
)
from app.engine.db_adapters.errors import (
    ConnectionFailedError,
    NotReadOnlyError,
    QueryExecutionError,
)
from app.engine.db_adapters.readonly import assert_read_only_sql, wrap_with_row_limit

# Introspecting a database with thousands of tables would produce a
# schema index nobody benefits from and a very slow registration request.
MAX_TABLES_INTROSPECTED = 200


def _first_line(exc: BaseException) -> str:
    """Driver exceptions are often multi-paragraph with connection details
    in them. Keep the first line only - enough to diagnose, short enough to
    show a user inside a chat reply."""
    text = str(exc).strip().splitlines()
    return text[0].strip() if text else exc.__class__.__name__


class SQLAlchemyAdapter(DBAdapter):
    """Base class for the three server-based SQL engines."""

    engine_name = ""
    #: SQLAlchemy dialect+driver prefix, e.g. "postgresql+psycopg2"
    driver_url_prefix = ""
    default_port: Optional[int] = None

    # --- subclass hooks ---------------------------------------------------

    def _build_url(self, conn: ConnectionInfo) -> URL:
        """URL.create() (not string formatting) so passwords containing
        `@`, `/`, `:` or other URL-significant characters are escaped
        correctly rather than producing a mangled DSN."""
        return URL.create(
            self.driver_url_prefix,
            username=conn.username or None,
            password=conn.password or None,
            host=conn.host or None,
            port=conn.port or self.default_port,
            database=conn.database,
        )

    def _connect_args(self, conn: ConnectionInfo, timeout_seconds: int) -> Dict[str, Any]:
        return {}

    def _pre_begin_statements(self, timeout_seconds: int) -> List[str]:
        """Session settings applied before the transaction opens. Executed
        best-effort: each failure is ignored (see class docstring)."""
        return []

    def _in_transaction_statements(self, timeout_seconds: int) -> List[str]:
        """Settings that must hold for the query. Failures propagate."""
        return []

    def _sample_rows_sql(self, qualified_table: str, limit: int) -> str:
        return f"SELECT * FROM {qualified_table} LIMIT {int(limit)}"

    # --- engine construction ----------------------------------------------

    def _create_engine(self, conn: ConnectionInfo, timeout_seconds: int) -> Engine:
        try:
            return create_engine(
                self._build_url(conn),
                connect_args=self._connect_args(conn, timeout_seconds),
                # NullPool: this engine exists for one request and is
                # disposed at the end of it. Pooling a *user's* external
                # database across requests would hold their credentials and
                # a live socket open far longer than needed, and would let
                # one request's session settings leak into another's.
                poolclass=NullPool,
                future=True,
            )
        except Exception as exc:  # noqa: BLE001 - bad URL/driver, not a query error
            raise ConnectionFailedError(
                f"Could not prepare a connection to the database: {_first_line(exc)}"
            ) from exc

    # --- DBAdapter -----------------------------------------------------------

    def test_connection(self, conn: ConnectionInfo) -> Tuple[bool, Optional[str]]:
        engine = None
        try:
            engine = self._create_engine(conn, timeout_seconds=10)
            with engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1").fetchall()
            return True, None
        except Exception as exc:  # noqa: BLE001 - contract: never raises
            return False, f"Could not connect: {_first_line(exc)}"
        finally:
            if engine is not None:
                engine.dispose()

    def introspect_schema(self, conn: ConnectionInfo) -> List[TableSchema]:
        engine = self._create_engine(conn, timeout_seconds=30)
        try:
            inspector = inspect(engine)
            try:
                names = list(inspector.get_table_names())
                names.extend(
                    name for name in inspector.get_view_names() if name not in names
                )
            except SQLAlchemyError as exc:
                raise QueryExecutionError(
                    f"Could not list the database's tables: {_first_line(exc)}"
                ) from exc

            preparer = engine.dialect.identifier_preparer
            tables: List[TableSchema] = []

            with engine.connect() as connection:
                for table_name in names[:MAX_TABLES_INTROSPECTED]:
                    columns = self._introspect_columns(inspector, table_name)
                    sample_rows = self._sample_rows(
                        connection, preparer.quote(table_name)
                    )
                    tables.append(
                        TableSchema(
                            name=table_name, columns=columns, sample_rows=sample_rows
                        )
                    )
            return tables
        except QueryExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise QueryExecutionError(
                f"Could not read the database schema: {_first_line(exc)}"
            ) from exc
        finally:
            engine.dispose()

    def _introspect_columns(self, inspector, table_name: str) -> List[ColumnSchema]:
        try:
            raw_columns = inspector.get_columns(table_name)
        except SQLAlchemyError:
            return []

        try:
            pk_names = set(inspector.get_pk_constraint(table_name).get("constrained_columns") or [])
        except SQLAlchemyError:
            pk_names = set()

        # column name -> "other_table.other_column"
        fk_targets: Dict[str, str] = {}
        try:
            for fk in inspector.get_foreign_keys(table_name):
                referred_table = fk.get("referred_table") or ""
                referred_columns = fk.get("referred_columns") or []
                for index, column in enumerate(fk.get("constrained_columns") or []):
                    target_column = (
                        referred_columns[index] if index < len(referred_columns) else ""
                    )
                    fk_targets[column] = f"{referred_table}.{target_column}".strip(".")
        except SQLAlchemyError:
            pass

        return [
            ColumnSchema(
                name=column["name"],
                type=str(column.get("type", "")),
                nullable=bool(column.get("nullable", True)),
                is_pk=column["name"] in pk_names,
                is_fk=column["name"] in fk_targets,
                references=fk_targets.get(column["name"]),
            )
            for column in raw_columns
        ]

    def _sample_rows(self, connection: Connection, qualified_table: str) -> List[Dict[str, Any]]:
        """Up to SAMPLE_ROW_LIMIT real rows, for embedding context.

        Best-effort: a table the credentials can't SELECT from, or one with
        a type the driver can't render, still contributes its column list
        to the schema index - it just has no examples attached."""
        sql = self._sample_rows_sql(qualified_table, SAMPLE_ROW_LIMIT)
        transaction = connection.begin()
        try:
            result = connection.exec_driver_sql(sql)
            columns = list(result.keys())
            rows = result.fetchmany(SAMPLE_ROW_LIMIT)
            return [
                {column: json_safe(value) for column, value in zip(columns, row)}
                for row in rows
            ]
        except Exception:  # noqa: BLE001 - samples are a nice-to-have
            return []
        finally:
            # Never commit, even for a plain SELECT - same rule as
            # execute_read_only below.
            transaction.rollback()

    def execute_read_only(
        self,
        conn: ConnectionInfo,
        query: str,
        max_rows: int,
        timeout_seconds: int,
    ) -> Tuple[List[str], List[List[Any]]]:
        # Layer 1-3: static guard. Raises NotReadOnlyError before anything
        # is opened, so a rejected query never even reaches the network.
        cleaned = assert_read_only_sql(query)
        max_rows = max(1, int(max_rows))

        engine = self._create_engine(conn, timeout_seconds)
        try:
            try:
                connection = engine.connect()
            except Exception as exc:  # noqa: BLE001
                raise ConnectionFailedError(
                    f"Could not connect to the database: {_first_line(exc)}"
                ) from exc

            with connection:
                pre_begin = self._pre_begin_statements(timeout_seconds)
                for statement in pre_begin:
                    try:
                        connection.exec_driver_sql(statement)
                    except Exception:  # noqa: BLE001 - best effort, see class docstring
                        pass
                if pre_begin:
                    # SQLAlchemy 2.0 opens an IMPLICIT transaction on the
                    # first execute, and the explicit connection.begin()
                    # below would then raise "a transaction is already
                    # begun". Close it here. Nothing is lost: these are
                    # SET SESSION statements, which are not transactional -
                    # a rollback doesn't undo them.
                    try:
                        connection.rollback()
                    except Exception:  # noqa: BLE001
                        pass

                # Layer 5a: let the database itself cap the rows where the
                # query shape allows it.
                wrapped = wrap_with_row_limit(cleaned, max_rows, self.engine_name)
                if wrapped is not None:
                    try:
                        return self._run_in_rolled_back_transaction(
                            connection, wrapped, max_rows, timeout_seconds
                        )
                    except NotReadOnlyError:
                        raise
                    except Exception:  # noqa: BLE001
                        # Layer 5b: some legitimate query shapes can't be
                        # wrapped on some engines (a duplicate column name
                        # in the select list makes the derived table
                        # invalid, for instance). Fall back to running it
                        # as written and truncating in Python - the cap is
                        # still enforced, just client-side.
                        pass

                return self._run_in_rolled_back_transaction(
                    connection, cleaned, max_rows, timeout_seconds
                )
        finally:
            engine.dispose()

    def _run_in_rolled_back_transaction(
        self,
        connection: Connection,
        sql: str,
        max_rows: int,
        timeout_seconds: int,
    ) -> Tuple[List[str], List[List[Any]]]:
        """Layer 4: the query runs inside an explicit transaction that is
        ALWAYS rolled back and never committed, whatever happens. Combined
        with the static guard this means that even a hypothetical bypass
        (a write hidden behind a function call, say) leaves nothing behind
        on any engine with transactional DDL/DML."""
        transaction = connection.begin()
        try:
            for statement in self._in_transaction_statements(timeout_seconds):
                connection.exec_driver_sql(statement)

            result = connection.exec_driver_sql(sql)
            if not result.returns_rows:
                # A read-only statement that returns nothing is odd but not
                # an error - report it as an empty result rather than
                # blowing up on result.keys().
                return [], []

            columns = [str(key) for key in result.keys()]
            rows = result.fetchmany(max_rows)
            return columns, [[json_safe(value) for value in row] for row in rows]
        except SQLAlchemyError as exc:
            raise QueryExecutionError(
                f"The database rejected the query: {_first_line(exc)}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - never leak a raw driver error
            raise QueryExecutionError(
                f"The query could not be run: {_first_line(exc)}"
            ) from exc
        finally:
            transaction.rollback()
