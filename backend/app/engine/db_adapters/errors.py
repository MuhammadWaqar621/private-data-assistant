"""
Exception types every adapter in this package raises instead of letting a
raw driver exception escape.

The rule (enforced by every adapter's `execute_read_only`): a caller in
app/api/ only ever has to handle these three types, and their `str()` is
always something safe and useful to show a user - never a psycopg2/
pymysql/pymssql/pymongo traceback, a connection string, or a credential.

  - NotReadOnlyError  - the query was rejected BEFORE touching the
                        database, because it is (or might be) a write.
                        This is a guard decision, not a database error.
  - QueryExecutionError - the query was accepted by the guard but the
                        database refused it (syntax error, missing table,
                        permission denied, timeout, connection failure).
  - ConnectionFailedError - could not connect at all (subclass of
                        QueryExecutionError so a caller that only cares
                        "the query didn't run" can catch one type).

Stdlib-only on purpose (no driver imports), so this module and
readonly.py can be imported and unit-tested without any database driver
installed - see tests/test_db_adapters_readonly.py.
"""


class AdapterError(RuntimeError):
    """Base class for every error this package raises deliberately."""


class NotReadOnlyError(AdapterError):
    """The query was rejected by the read-only guard before execution."""


class QueryExecutionError(AdapterError):
    """The database rejected or failed the query (already made
    human-readable - never a raw driver traceback)."""


class ConnectionFailedError(QueryExecutionError):
    """Could not establish a connection to the user's database."""


class UnsupportedEngineError(AdapterError):
    """`get_adapter()` was handed an engine name with no adapter."""
