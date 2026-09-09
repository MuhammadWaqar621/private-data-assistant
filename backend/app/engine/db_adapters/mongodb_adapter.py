"""
MongoDB adapter (pymongo).

MongoDB is the one non-SQL engine here, so two things work differently from
the other four adapters.

**1. There is no schema to introspect.** A collection is just documents, so
`introspect_schema()` samples MONGO_SCHEMA_SAMPLE_SIZE (20) documents per
collection and infers a lightweight field schema from them: for each field
seen, the set of BSON/Python types observed across the sample (rendered as
e.g. `string`, `int | null`, `array<object>`). Those inferred fields are
reported through the same `ColumnSchema`/`TableSchema` structures the SQL
adapters use, so `schema_rag.py` embeds Mongo collections and SQL tables
with identical machinery. Because the schema is inferred, it is explicitly
a *sample*: a field that only appears in old documents outside the sample
window won't be listed, which is worth knowing when a question about it
comes back "I don't see that field".

**2. The `query` argument is JSON, not SQL.** The model is told (in
AGENT_SYSTEM_PROMPT, conditioned on the connection's engine) to emit an
operation spec:

    {"operation": "find", "collection": "orders",
     "filter": {"status": "shipped"}, "projection": {"_id": 0},
     "sort": {"total": -1}, "limit": 20}
    {"operation": "aggregate", "collection": "orders",
     "pipeline": [{"$group": {"_id": "$status", "n": {"$sum": 1}}}]}
    {"operation": "count",    "collection": "orders", "filter": {...}}
    {"operation": "distinct", "collection": "orders", "field": "status"}

`readonly.validate_mongo_operation()` does the enforcement: only those four
operations are permitted, `$out`/`$merge`/`$function`/`$accumulator`/
`$where` are refused anywhere (including nested inside a `find` filter, not
just as a top-level pipeline stage - a `$where` in a filter runs the same
server-side JavaScript), and the row cap is folded in (`.limit()` for find,
an appended `$limit` stage for aggregate). `maxTimeMS` bounds every call
server-side.

Results are flattened into the same `(columns, rows)` shape the SQL
adapters return - the column list is the union of the keys present in the
returned documents, in first-seen order - so `rag.py` and the chart
renderer never need to care which engine produced the rows.

`extra_params` understood here:
  {"auth_source": "admin"}   -> the database to authenticate against
  {"ssl_mode": "require"}    -> connect over TLS
  {"replica_set": "rs0"}     -> replica set name
  {"direct_connection": true}-> bypass topology discovery (handy for a
                                single node behind a port mapping)
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from app.engine.db_adapters.base import (
    MONGO_SCHEMA_SAMPLE_SIZE,
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
from app.engine.db_adapters.readonly import validate_mongo_operation

DEFAULT_PORT = 27017
MAX_COLLECTIONS_INTROSPECTED = 200


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        inner = sorted({_type_name(item) for item in value[:5]}) or ["unknown"]
        return f"array<{' | '.join(inner)}>"
    if isinstance(value, dict):
        return "object"
    # ObjectId, datetime, Decimal128, Binary, ...
    return type(value).__name__


class MongoDBAdapter(DBAdapter):
    engine_name = "mongodb"

    # --- connection --------------------------------------------------------

    def _client(self, conn: ConnectionInfo, timeout_seconds: int):
        try:
            from pymongo import MongoClient
        except ImportError as exc:  # pragma: no cover - driver always installed
            raise ConnectionFailedError(
                "MongoDB support isn't available in this deployment (pymongo "
                "is not installed)."
            ) from exc

        extra = conn.extra_params or {}
        milliseconds = max(1000, int(timeout_seconds) * 1000)

        kwargs: Dict[str, Any] = {
            "host": conn.host or "localhost",
            "port": int(conn.port or DEFAULT_PORT),
            "serverSelectionTimeoutMS": min(milliseconds, 15000),
            "connectTimeoutMS": min(milliseconds, 15000),
            "socketTimeoutMS": milliseconds,
            "appname": "private-data-assistant",
        }
        if conn.username:
            kwargs["username"] = conn.username
        if conn.password:
            kwargs["password"] = conn.password
        if extra.get("auth_source"):
            kwargs["authSource"] = str(extra["auth_source"])
        if extra.get("auth_mechanism"):
            kwargs["authMechanism"] = str(extra["auth_mechanism"])
        if extra.get("replica_set"):
            kwargs["replicaSet"] = str(extra["replica_set"])
        if extra.get("direct_connection"):
            kwargs["directConnection"] = True
        if str(extra.get("ssl_mode") or "").strip().lower() in {
            "require",
            "required",
            "true",
            "1",
            "yes",
        }:
            kwargs["tls"] = True

        try:
            return MongoClient(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise ConnectionFailedError(
                f"Could not prepare a MongoDB connection: {exc}"
            ) from exc

    # --- DBAdapter -----------------------------------------------------------

    def test_connection(self, conn: ConnectionInfo) -> Tuple[bool, Optional[str]]:
        client = None
        try:
            client = self._client(conn, timeout_seconds=10)
            # Ping the target database rather than `admin`: credentials
            # scoped to one database can't run commands against admin, and
            # refusing such a connection would be wrong.
            client[conn.database].command("ping")
            return True, None
        except Exception as exc:  # noqa: BLE001 - contract: never raises
            return False, f"Could not connect: {str(exc).splitlines()[0]}"
        finally:
            if client is not None:
                client.close()

    def introspect_schema(self, conn: ConnectionInfo) -> List[TableSchema]:
        client = self._client(conn, timeout_seconds=30)
        try:
            database = client[conn.database]
            try:
                names = sorted(database.list_collection_names())
            except Exception as exc:  # noqa: BLE001
                raise QueryExecutionError(
                    f"Could not list the database's collections: {str(exc).splitlines()[0]}"
                ) from exc

            tables: List[TableSchema] = []
            for name in names[:MAX_COLLECTIONS_INTROSPECTED]:
                try:
                    documents = list(
                        database[name].find({}, limit=MONGO_SCHEMA_SAMPLE_SIZE)
                    )
                except Exception:  # noqa: BLE001 - one unreadable collection
                    documents = []

                tables.append(
                    TableSchema(
                        name=name,
                        columns=self._infer_columns(documents),
                        sample_rows=[
                            json_safe(document) for document in documents[:SAMPLE_ROW_LIMIT]
                        ],
                    )
                )
            return tables
        finally:
            client.close()

    def _infer_columns(self, documents: List[Dict[str, Any]]) -> List[ColumnSchema]:
        """Field name -> observed type(s), in first-seen order.

        `nullable` here means "absent from, or null in, at least one of the
        sampled documents" - the closest honest analogue of a SQL nullable
        column for a schemaless store. `_id` is reported as the primary
        key, which it always is in MongoDB."""
        observed: Dict[str, set] = {}
        order: List[str] = []
        for document in documents:
            for key, value in document.items():
                if key not in observed:
                    observed[key] = set()
                    order.append(key)
                observed[key].add(_type_name(value))

        total = len(documents)
        columns: List[ColumnSchema] = []
        for key in order:
            present = sum(1 for document in documents if key in document)
            types = sorted(observed[key])
            columns.append(
                ColumnSchema(
                    name=key,
                    type=" | ".join(types),
                    nullable=present < total or "null" in types,
                    is_pk=(key == "_id"),
                    is_fk=False,
                    references=None,
                )
            )
        return columns

    def execute_read_only(
        self,
        conn: ConnectionInfo,
        query: str,
        max_rows: int,
        timeout_seconds: int,
    ) -> Tuple[List[str], List[List[Any]]]:
        # Raises NotReadOnlyError / QueryExecutionError before any
        # connection is opened - see readonly.validate_mongo_operation().
        spec = validate_mongo_operation(query, max_rows)
        max_time_ms = max(1000, int(timeout_seconds) * 1000)

        client = self._client(conn, timeout_seconds)
        try:
            database = client[conn.database]
            collection = database[spec["collection"]]
            operation = spec["operation"]

            try:
                if operation == "find":
                    cursor = collection.find(
                        spec["filter"],
                        projection=spec.get("projection"),
                        limit=spec["limit"],
                        skip=spec.get("skip", 0),
                        max_time_ms=max_time_ms,
                    )
                    if spec.get("sort"):
                        cursor = cursor.sort(list(spec["sort"].items()))
                    return _documents_to_rows(list(cursor))

                if operation == "aggregate":
                    cursor = collection.aggregate(
                        spec["pipeline"], maxTimeMS=max_time_ms
                    )
                    return _documents_to_rows(list(cursor)[: spec["limit"]])

                if operation == "count":
                    count = collection.count_documents(
                        spec["filter"], maxTimeMS=max_time_ms
                    )
                    return ["count"], [[count]]

                if operation == "distinct":
                    values = collection.distinct(
                        spec["field"], spec["filter"], maxTimeMS=max_time_ms
                    )
                    return [spec["field"]], [
                        [json_safe(value)] for value in values[: spec["limit"]]
                    ]
            except NotReadOnlyError:
                raise
            except Exception as exc:  # noqa: BLE001 - never leak a driver error
                raise QueryExecutionError(
                    f"The database rejected the query: {str(exc).splitlines()[0]}"
                ) from exc

            # Unreachable: validate_mongo_operation() already whitelisted
            # the operation, but an explicit failure beats a silent None.
            raise QueryExecutionError(f"Unsupported MongoDB operation: {operation}")
        finally:
            client.close()


def _documents_to_rows(
    documents: List[Dict[str, Any]]
) -> Tuple[List[str], List[List[Any]]]:
    """Flatten a list of BSON documents into (columns, rows).

    Columns are the union of every document's keys in first-seen order, so
    a heterogeneous result set still tabulates cleanly; a document missing
    a key gets None in that position. Nested objects/arrays are JSON-encoded
    into a single cell rather than exploded, which keeps the row rectangular
    and readable both for the model and for a chart."""
    columns: List[str] = []
    for document in documents:
        for key in document:
            if key not in columns:
                columns.append(key)

    rows: List[List[Any]] = []
    for document in documents:
        row: List[Any] = []
        for column in columns:
            value = json_safe(document.get(column))
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            row.append(value)
        rows.append(row)
    return columns, rows
