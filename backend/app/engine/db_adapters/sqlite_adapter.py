"""
SQLite adapter - stdlib `sqlite3`, operating on a database FILE this
application stores itself.

SQLite is the one engine where there is no server to connect to: the user
uploads a `.sqlite`/`.db` file, `app/api/connections.py` writes it to
`storage/{user_id}/{connection_id}/database.sqlite` (mirroring the sibling
project's document-storage convention), and `extra_params["storage_path"]`
records where. That path is produced by the server from the authenticated
user id and the row's own id - it is never taken from client input - and
this adapter refuses anything that isn't an existing file.

Read-only story - in one respect the strongest of the five, in another the
weakest:

  * the file is opened through a URI with `mode=ro`, so SQLite itself
    refuses every write at the C level. This is a hard guarantee that does
    not depend on our SQL parsing at all: an `ATTACH`, a `PRAGMA
    journal_mode=WAL`, a `CREATE TABLE` - all fail with "attempt to write a
    readonly database" even if they somehow got past the guard.
  * the static guard (readonly.py) still runs first, so the model gets a
    clear explanation rather than a driver error;
  * row cap via `SELECT * FROM (<query>) AS _sub LIMIT n`;
  * `conn.rollback()` at the end regardless (belt and braces - nothing can
    have been written anyway);
  * **timeouts are the weak spot.** SQLite has no statement timeout: a
    query runs inside the calling thread until it finishes. Rather than a
    thread that would be abandoned but keep running, this adapter installs
    a `set_progress_handler` callback that fires every N virtual-machine
    instructions and returns non-zero once the deadline passes, which makes
    SQLite genuinely ABORT the statement (raising
    `sqlite3.OperationalError`). A pathological query that spends all its
    time inside a single long-running C call can still overrun, but for
    ordinary scans this is a real interrupt rather than a best-effort one.

Introspection uses `PRAGMA table_info` / `PRAGMA foreign_key_list` rather
than SQLAlchemy's Inspector: this adapter deliberately has no SQLAlchemy
dependency (see class docstring in sql_common.py for why SQLite is not
built on that base), and the PRAGMAs give exactly the same facts. Note
that PRAGMA appears on the read-only blocklist - that blocklist governs
*model-written* queries, not this module's own fixed introspection SQL.
"""

import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

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

# How often (in SQLite virtual-machine instructions) the deadline check
# runs. Small enough to interrupt promptly, large enough that the callback
# overhead is noise.
_PROGRESS_HANDLER_INSTRUCTIONS = 1000

MAX_TABLES_INTROSPECTED = 200


def _storage_path(conn: ConnectionInfo) -> str:
    path = (conn.extra_params or {}).get("storage_path") or conn.database
    if not path:
        raise ConnectionFailedError(
            "This SQLite connection has no database file recorded - re-upload "
            "the .sqlite file to fix it."
        )
    return str(path)


class SQLiteAdapter(DBAdapter):
    engine_name = "sqlite"

    # --- connection --------------------------------------------------------

    def _connect(self, conn: ConnectionInfo, timeout_seconds: int) -> sqlite3.Connection:
        path = _storage_path(conn)
        if not os.path.isfile(path):
            raise ConnectionFailedError(
                "The uploaded SQLite database file is missing from the "
                "server's storage - re-upload it to fix this connection."
            )
        # mode=ro: SQLite refuses writes itself, independently of our guard.
        uri = f"file:{path}?mode=ro"
        try:
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=max(1, int(timeout_seconds)),
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise ConnectionFailedError(
                f"Could not open the SQLite database file: {exc}"
            ) from exc
        connection.row_factory = sqlite3.Row
        return connection

    def _install_deadline(self, connection: sqlite3.Connection, timeout_seconds: int) -> None:
        deadline = time.monotonic() + max(1, int(timeout_seconds))

        def _abort_if_expired() -> int:
            # A non-zero return makes SQLite abort the running statement.
            return 1 if time.monotonic() > deadline else 0

        connection.set_progress_handler(_abort_if_expired, _PROGRESS_HANDLER_INSTRUCTIONS)

    # --- DBAdapter -----------------------------------------------------------

    def test_connection(self, conn: ConnectionInfo) -> Tuple[bool, Optional[str]]:
        connection = None
        try:
            connection = self._connect(conn, timeout_seconds=10)
            connection.execute("SELECT 1").fetchall()
            return True, None
        except Exception as exc:  # noqa: BLE001 - contract: never raises
            return False, f"Could not open the SQLite database: {exc}"
        finally:
            if connection is not None:
                connection.close()

    def introspect_schema(self, conn: ConnectionInfo) -> List[TableSchema]:
        connection = self._connect(conn, timeout_seconds=30)
        try:
            names = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                ).fetchall()
            ]

            tables: List[TableSchema] = []
            for table_name in names[:MAX_TABLES_INTROSPECTED]:
                tables.append(
                    TableSchema(
                        name=table_name,
                        columns=self._introspect_columns(connection, table_name),
                        sample_rows=self._sample_rows(connection, table_name),
                    )
                )
            return tables
        except sqlite3.Error as exc:
            raise QueryExecutionError(f"Could not read the SQLite schema: {exc}") from exc
        finally:
            connection.close()

    def _introspect_columns(
        self, connection: sqlite3.Connection, table_name: str
    ) -> List[ColumnSchema]:
        quoted = _quote_identifier(table_name)
        try:
            info = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
        except sqlite3.Error:
            return []

        fk_targets: Dict[str, str] = {}
        try:
            for fk in connection.execute(f"PRAGMA foreign_key_list({quoted})").fetchall():
                # columns: id, seq, table, from, to, on_update, on_delete, match
                fk_targets[fk["from"]] = f"{fk['table']}.{fk['to'] or ''}".strip(".")
        except sqlite3.Error:
            pass

        return [
            ColumnSchema(
                name=row["name"],
                type=row["type"] or "",
                nullable=not bool(row["notnull"]),
                is_pk=bool(row["pk"]),
                is_fk=row["name"] in fk_targets,
                references=fk_targets.get(row["name"]),
            )
            for row in info
        ]

    def _sample_rows(
        self, connection: sqlite3.Connection, table_name: str
    ) -> List[Dict[str, Any]]:
        try:
            cursor = connection.execute(
                f"SELECT * FROM {_quote_identifier(table_name)} LIMIT {SAMPLE_ROW_LIMIT}"
            )
            columns = [description[0] for description in cursor.description or []]
            return [
                {column: json_safe(value) for column, value in zip(columns, row)}
                for row in cursor.fetchmany(SAMPLE_ROW_LIMIT)
            ]
        except sqlite3.Error:
            return []

    def execute_read_only(
        self,
        conn: ConnectionInfo,
        query: str,
        max_rows: int,
        timeout_seconds: int,
    ) -> Tuple[List[str], List[List[Any]]]:
        cleaned = assert_read_only_sql(query)
        max_rows = max(1, int(max_rows))

        connection = self._connect(conn, timeout_seconds)
        try:
            self._install_deadline(connection, timeout_seconds)

            wrapped = wrap_with_row_limit(cleaned, max_rows, self.engine_name)
            if wrapped is not None:
                try:
                    return self._run(connection, wrapped, max_rows)
                except NotReadOnlyError:
                    raise
                except QueryExecutionError:
                    # Same fallback as the SQLAlchemy adapters: a query
                    # shape that can't be wrapped is run as written and
                    # truncated in Python instead.
                    pass

            return self._run(connection, cleaned, max_rows)
        finally:
            try:
                connection.rollback()
            finally:
                connection.set_progress_handler(None, 0)
                connection.close()

    def _run(
        self, connection: sqlite3.Connection, sql: str, max_rows: int
    ) -> Tuple[List[str], List[List[Any]]]:
        try:
            cursor = connection.execute(sql)
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise QueryExecutionError(
                    "The query took too long and was stopped. Try narrowing it "
                    "down (add a filter, or select fewer rows)."
                ) from exc
            raise QueryExecutionError(f"The database rejected the query: {exc}") from exc
        except sqlite3.Error as exc:
            raise QueryExecutionError(f"The query could not be run: {exc}") from exc

        columns = [description[0] for description in cursor.description or []]
        try:
            rows = cursor.fetchmany(max_rows)
        except sqlite3.Error as exc:
            raise QueryExecutionError(f"The query failed while reading rows: {exc}") from exc
        return columns, [[json_safe(value) for value in row] for row in rows]


def _quote_identifier(name: str) -> str:
    """Double-quote an identifier for interpolation into a PRAGMA/SELECT.

    Table names here come from `sqlite_master`, not from user input, but
    they can still contain spaces or quotes, and PRAGMA doesn't accept bind
    parameters - so they're quoted properly rather than trusted."""
    return '"' + str(name).replace('"', '""') + '"'
