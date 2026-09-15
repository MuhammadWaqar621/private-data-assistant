"""
The common interface every database adapter implements, plus the plain
data structures that cross the boundary.

Stdlib-only on purpose (dataclasses + abc, no driver imports), so this
module can be imported anywhere - including by tests that have no database
driver installed.

Contract for implementers:

  * `test_connection(conn)` -> `(ok, error_message)`. Must actually
    connect and run a trivial statement (`SELECT 1`, or a Mongo `ping`),
    and must NEVER raise: a failure is reported as `(False, "why")`, with
    a message safe to show the user.
  * `introspect_schema(conn)` -> `list[TableSchema]`. One entry per table
    (or collection), with column metadata and up to
    `SAMPLE_ROW_LIMIT` sample rows. May raise - the caller
    (app/api/connections.py) turns that into `status=failed`.
  * `execute_read_only(conn, query, max_rows, timeout_seconds)` ->
    `(columns, rows)`. Must enforce read-only (see readonly.py), must cap
    rows, must apply a timeout, and must raise only NotReadOnlyError /
    QueryExecutionError / ConnectionFailedError - never a raw driver
    exception.

`ConnectionInfo.password` is PLAINTEXT and arrives already decrypted: this
package has no idea credentials are stored encrypted (that is
app/core/crypto.py's job - see app/engine/__init__.py's isolation
contract). Instances are short-lived, built immediately before use.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# How many real rows are pulled from each table during introspection and
# embedded alongside its column list. Small on purpose: enough for the
# model to see the shape/format of the data (date formats, enum-ish string
# values, units) without turning the schema index into a data export.
SAMPLE_ROW_LIMIT = 5

# How many documents a MongoDB collection is sampled from to infer a field
# schema (Mongo has no declared schema to introspect).
MONGO_SCHEMA_SAMPLE_SIZE = 20


@dataclass(frozen=True)
class ConnectionInfo:
    """Everything needed to open one connection to a user's database.

    Built by app/api/connections.py from a DatabaseConnection row plus the
    just-decrypted password. Never persisted, never logged, never
    serialized into a response.
    """

    engine: str
    database: str
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    # Engine-specific options - see app/models/database_connection.py:
    #   postgres/mysql/mssql: {"ssl_mode": "require"}
    #   mongodb:              {"auth_source": "admin"}
    #   sqlite:               {"storage_url": "https://<store>.public.blob.vercel-storage.com/1/2/database.sqlite"}
    #                         (or {"storage_path": "<local file>"} for local
    #                         development without a Vercel Blob store)
    extra_params: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ColumnSchema:
    name: str
    type: str
    nullable: bool
    is_pk: bool = False
    is_fk: bool = False
    # "other_table.other_column" when is_fk, else None.
    references: Optional[str] = None


@dataclass(frozen=True)
class TableSchema:
    """One table (SQL) or collection (MongoDB), as introspected.

    `sample_rows` are plain JSON-safe dicts - adapters are responsible for
    coercing driver-specific types (dates, Decimals, ObjectIds, bytes) into
    something `json.dumps`-able, because these end up both in the embedded
    text and, potentially, in a tool result shown to the model.
    """

    name: str
    columns: List[ColumnSchema] = field(default_factory=list)
    sample_rows: List[Dict[str, Any]] = field(default_factory=list)


class DBAdapter(ABC):
    """One implementation per supported engine. Stateless: every method
    takes the ConnectionInfo it needs and opens/closes its own connection,
    so adapters are safe to share and cheap to construct."""

    #: matches DatabaseEngine's value in app/models/database_connection.py
    engine_name: str = ""

    @abstractmethod
    def test_connection(self, conn: ConnectionInfo) -> Tuple[bool, Optional[str]]:
        """Connect and run a trivial statement. Never raises."""

    @abstractmethod
    def introspect_schema(self, conn: ConnectionInfo) -> List[TableSchema]:
        """Every table/collection with its columns/fields and sample rows."""

    @abstractmethod
    def execute_read_only(
        self,
        conn: ConnectionInfo,
        query: str,
        max_rows: int,
        timeout_seconds: int,
    ) -> Tuple[List[str], List[List[Any]]]:
        """Run a guaranteed-read-only query, returning (columns, rows)."""


def json_safe(value: Any) -> Any:
    """Coerce one driver value into something JSON-serializable.

    Shared by every adapter so a Decimal from Postgres, a `datetime` from
    MySQL, a `bytes` blob from SQLite and an `ObjectId` from MongoDB all
    reach the model (and the SSE stream, and `messages.chart_spec`) in the
    same shape. Anything unrecognized falls back to `str()` rather than
    exploding at serialization time.
    """
    import datetime
    import decimal
    import uuid

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, decimal.Decimal):
        # float() would silently lose precision on money-shaped values;
        # a string keeps the exact digits the database returned, and the
        # model reads it the same way either way.
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"<{len(raw)} bytes>"
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    return str(value)
