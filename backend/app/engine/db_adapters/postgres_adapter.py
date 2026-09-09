"""
PostgreSQL adapter (psycopg2, via SQLAlchemy).

Read-only story - the strongest of the five engines, because Postgres has
a real server-side switch:

  * static guard (readonly.py) rejects anything that isn't a single
    SELECT/WITH before connecting;
  * `SET TRANSACTION READ ONLY` is issued as the first statement inside
    the transaction, so the SERVER refuses any write for the rest of it,
    with no reliance on our parsing being perfect;
  * `SET LOCAL statement_timeout` gives a true server-side cancel of a
    long-running query (LOCAL = scoped to this transaction, so it can't
    leak into anything else on the connection);
  * the row cap is applied as `SELECT * FROM (<query>) AS _sub LIMIT n`,
    which Postgres accepts even when `<query>` is a `WITH ...` CTE;
  * the transaction is always rolled back, never committed.

`extra_params` understood here:
  {"ssl_mode": "require"}  -> psycopg2's `sslmode` (disable / allow /
                              prefer / require / verify-ca / verify-full)
"""

from typing import Any, Dict, List

from app.engine.db_adapters.base import ConnectionInfo
from app.engine.db_adapters.sql_common import SQLAlchemyAdapter

DEFAULT_PORT = 5432


class PostgresAdapter(SQLAlchemyAdapter):
    engine_name = "postgres"
    driver_url_prefix = "postgresql+psycopg2"
    default_port = DEFAULT_PORT

    def _connect_args(self, conn: ConnectionInfo, timeout_seconds: int) -> Dict[str, Any]:
        args: Dict[str, Any] = {
            # Seconds. Bounded so a wrong host doesn't hang the request for
            # the OS-default TCP timeout.
            "connect_timeout": max(3, min(int(timeout_seconds), 15)),
            # Shows up in pg_stat_activity, so a DBA looking at their own
            # server can see exactly what these connections are.
            "application_name": "private-data-assistant",
        }
        ssl_mode = (conn.extra_params or {}).get("ssl_mode")
        if ssl_mode:
            args["sslmode"] = str(ssl_mode)
        return args

    def _in_transaction_statements(self, timeout_seconds: int) -> List[str]:
        milliseconds = max(1000, int(timeout_seconds) * 1000)
        return [
            "SET TRANSACTION READ ONLY",
            f"SET LOCAL statement_timeout = {milliseconds}",
        ]
