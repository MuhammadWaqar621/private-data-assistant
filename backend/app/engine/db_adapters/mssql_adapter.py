"""
SQL Server / MSSQL adapter (pymssql, via SQLAlchemy).

**Driver choice - pymssql, not pyodbc (a deliberate tradeoff).** pyodbc is
the more featureful and more commonly recommended SQL Server driver, but it
requires a system ODBC driver manager plus Microsoft's own
`msodbcsql18`/FreeTDS package installed in the image (an apt repository
key, an EULA acceptance flag, and ~200MB) before it can connect at all.
pymssql bundles FreeTDS in its wheel, so `pip install pymssql` is genuinely
all this container needs. What that costs us:

  * no `ApplicationIntent=ReadOnly` connection option (that is an ODBC/
    MS-driver feature), so unlike Postgres there is no server-side
    read-only switch to lean on - MSSQL read-only rests on the static
    guard plus the always-rolled-back transaction;
  * no Azure AD / Entra authentication, and no encrypted-by-default
    negotiation with modern TLS policies - a server that demands
    `Encrypt=yes` with a strict TLS version may refuse the connection.

Swapping to pyodbc later means changing `driver_url_prefix` and
`_connect_args` here and nothing else - which is the point of the adapter
boundary.

**The dialect difference that actually bites: TOP vs LIMIT.** T-SQL has no
`LIMIT` clause at all, so the row cap cannot be expressed the way it is for
the other three SQL engines. `readonly.wrap_with_row_limit()` emits
`SELECT TOP (n) * FROM (<query>) AS _sub` for this engine, and declines to
wrap two query shapes T-SQL forbids inside a derived table (a `WITH` CTE,
and an `ORDER BY` without its own TOP/OFFSET) - those fall back to being
run as written and truncated in Python. See that function's docstring.

Timeout: pymssql's `timeout` connect argument is a real per-query timeout
(it cancels the command), and `SET LOCK_TIMEOUT` additionally stops a query
from waiting forever on someone else's lock.

`extra_params` understood here:
  {"ssl_mode": "require"}  -> pymssql `tds_version`/encryption is
                              negotiated by FreeTDS; recorded and used to
                              request encryption where supported.
"""

from typing import Any, Dict, List

from app.engine.db_adapters.base import ConnectionInfo
from app.engine.db_adapters.sql_common import SQLAlchemyAdapter

DEFAULT_PORT = 1433


class MSSQLAdapter(SQLAlchemyAdapter):
    engine_name = "mssql"
    driver_url_prefix = "mssql+pymssql"
    default_port = DEFAULT_PORT

    def _connect_args(self, conn: ConnectionInfo, timeout_seconds: int) -> Dict[str, Any]:
        args: Dict[str, Any] = {
            # pymssql: `timeout` is the query timeout in seconds,
            # `login_timeout` is the connect timeout.
            "timeout": max(3, int(timeout_seconds)),
            "login_timeout": max(3, min(int(timeout_seconds), 15)),
            "appname": "private-data-assistant",
        }
        ssl_mode = str((conn.extra_params or {}).get("ssl_mode") or "").strip().lower()
        if ssl_mode in {"require", "required", "true", "1", "yes"}:
            # FreeTDS negotiates encryption per TDS version; 7.4 is the
            # first that supports it against modern SQL Server.
            args["tds_version"] = "7.4"
        return args

    def _pre_begin_statements(self, timeout_seconds: int) -> List[str]:
        milliseconds = max(1000, int(timeout_seconds) * 1000)
        return [f"SET LOCK_TIMEOUT {milliseconds}"]

    def _sample_rows_sql(self, qualified_table: str, limit: int) -> str:
        # T-SQL: TOP goes before the select list; there is no LIMIT.
        return f"SELECT TOP ({int(limit)}) * FROM {qualified_table}"
