"""
MySQL / MariaDB adapter (PyMySQL, via SQLAlchemy).

Read-only story:

  * static guard (readonly.py) first, same as every engine;
  * `SET SESSION TRANSACTION READ ONLY` is issued BEFORE the transaction
    opens - MySQL applies that statement to subsequent transactions, not
    the one already in progress, which is why it lives in
    `_pre_begin_statements()` rather than alongside Postgres' in-transaction
    `SET TRANSACTION READ ONLY`;
  * a statement timeout is attempted twice, because the two forks spell it
    differently and neither accepts the other's syntax:
      - MySQL 5.7.8+ : `SET SESSION MAX_EXECUTION_TIME = <milliseconds>`
                       (SELECT-only, which is exactly our case)
      - MariaDB      : `SET SESSION max_statement_time = <seconds>`
    Both are best-effort - an older server that rejects both still gets
    the connect timeout and the row cap, it just can't be interrupted
    mid-query;
  * row cap via `SELECT * FROM (<query>) AS _sub LIMIT n`;
  * always rolled back, never committed.

`extra_params` understood here:
  {"ssl_mode": "require"}   -> enable TLS
  {"ssl_ca": "/path/ca.pem"} -> enable TLS and verify against this CA
"""

from typing import Any, Dict, List

from app.engine.db_adapters.base import ConnectionInfo
from app.engine.db_adapters.sql_common import SQLAlchemyAdapter

DEFAULT_PORT = 3306

_TLS_MODES = {"require", "required", "verify-ca", "verify-full", "preferred", "true", "1", "yes"}


class MySQLAdapter(SQLAlchemyAdapter):
    engine_name = "mysql"
    driver_url_prefix = "mysql+pymysql"
    default_port = DEFAULT_PORT

    def _connect_args(self, conn: ConnectionInfo, timeout_seconds: int) -> Dict[str, Any]:
        extra = conn.extra_params or {}
        args: Dict[str, Any] = {
            "connect_timeout": max(3, min(int(timeout_seconds), 15)),
            # Socket-level read/write timeouts, so a server that accepts
            # the connection and then stalls still fails inside the
            # request's budget.
            "read_timeout": max(3, int(timeout_seconds)),
            "write_timeout": max(3, int(timeout_seconds)),
            "charset": "utf8mb4",
        }

        ssl_ca = extra.get("ssl_ca")
        ssl_mode = str(extra.get("ssl_mode") or "").strip().lower()
        if ssl_ca:
            args["ssl"] = {"ca": str(ssl_ca)}
        elif ssl_mode in _TLS_MODES:
            # Encrypt without pinning a CA - the equivalent of Postgres'
            # `sslmode=require`. Supply ssl_ca as well if the server's
            # certificate should actually be verified.
            args["ssl"] = {"check_hostname": False}
        return args

    def _pre_begin_statements(self, timeout_seconds: int) -> List[str]:
        milliseconds = max(1000, int(timeout_seconds) * 1000)
        seconds = max(1, int(timeout_seconds))
        return [
            "SET SESSION TRANSACTION READ ONLY",
            f"SET SESSION MAX_EXECUTION_TIME = {milliseconds}",  # MySQL 5.7.8+
            f"SET SESSION max_statement_time = {seconds}",  # MariaDB
        ]
