"""
Database adapters - one per supported engine, all behind the single
`DBAdapter` interface in base.py.

`get_adapter(engine)` is the ONE dispatch point in the codebase: nothing
outside this package should ever import a specific adapter module or
branch on an engine name. Add an engine by writing one module here and
adding one line to `_ADAPTER_MODULES` below (plus the enum value in
app/models/database_connection.py and a driver in requirements.txt).

Imports are deliberately LAZY (resolved inside `get_adapter`, not at
module import time) for two reasons:
  - a missing optional driver (pymssql on a machine with no FreeTDS, say)
    only breaks the engine that needs it, not the whole application;
  - `readonly.py`, `base.py` and `errors.py` stay importable with nothing
    but the standard library, which is what lets the read-only guard be
    unit-tested without any database installed.

This package obeys app/engine/'s isolation contract: no imports from
app.api / app.models / auth, plain values in and out, and configuration
(credentials, row caps, timeouts) passed in as arguments rather than read
from Settings.
"""

import importlib
from typing import Dict

from app.engine.db_adapters.base import (
    ColumnSchema,
    ConnectionInfo,
    DBAdapter,
    TableSchema,
)
from app.engine.db_adapters.errors import (
    AdapterError,
    ConnectionFailedError,
    NotReadOnlyError,
    QueryExecutionError,
    UnsupportedEngineError,
)

__all__ = [
    "get_adapter",
    "supported_engines",
    "DBAdapter",
    "ConnectionInfo",
    "TableSchema",
    "ColumnSchema",
    "AdapterError",
    "NotReadOnlyError",
    "QueryExecutionError",
    "ConnectionFailedError",
    "UnsupportedEngineError",
]

# engine name -> (module under this package, adapter class name)
_ADAPTER_MODULES: Dict[str, tuple] = {
    "postgres": ("postgres_adapter", "PostgresAdapter"),
    "mysql": ("mysql_adapter", "MySQLAdapter"),
    "mssql": ("mssql_adapter", "MSSQLAdapter"),
    "sqlite": ("sqlite_adapter", "SQLiteAdapter"),
    "mongodb": ("mongodb_adapter", "MongoDBAdapter"),
}

_INSTANCES: Dict[str, DBAdapter] = {}


def supported_engines() -> list:
    """The engine names `get_adapter()` accepts, sorted - used by the API
    layer's validation error messages and by the README."""
    return sorted(_ADAPTER_MODULES)


def get_adapter(engine: str) -> DBAdapter:
    """Return the (cached, stateless) adapter for `engine`.

    Raises UnsupportedEngineError for an unknown engine name, and
    ConnectionFailedError if the adapter's driver isn't installed - both
    with messages a user can act on, rather than an ImportError traceback.
    """
    key = (engine or "").strip().lower()
    if key not in _ADAPTER_MODULES:
        raise UnsupportedEngineError(
            f"'{engine}' is not a supported database engine. "
            f"Supported engines: {', '.join(supported_engines())}."
        )

    cached = _INSTANCES.get(key)
    if cached is not None:
        return cached

    module_name, class_name = _ADAPTER_MODULES[key]
    try:
        module = importlib.import_module(f"{__name__}.{module_name}")
    except ImportError as exc:  # driver missing from this deployment
        raise ConnectionFailedError(
            f"Support for '{key}' databases isn't available in this "
            f"deployment - its driver could not be loaded ({exc})."
        ) from exc

    adapter = getattr(module, class_name)()
    _INSTANCES[key] = adapter
    return adapter
