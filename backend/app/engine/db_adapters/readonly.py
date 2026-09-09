"""
Read-only enforcement - the security core of this project.

Everything here is a PURE FUNCTION over strings/dicts with no database
driver imported and no I/O, so it can be reasoned about and unit-tested
directly (tests/test_db_adapters_readonly.py) rather than only observed
through a live database. The adapters call into it; they never re-implement
any of it.

Why a guard at all: the assistant writes the query. An LLM that has been
told "only write SELECTs" is a *policy*, not a *control* - a prompt
injection hidden in a table name, a column comment, or the user's own
message could try to talk it into writing a DELETE. So no query reaches a
driver without passing these checks first, and even a query that passes
runs inside a transaction that is always rolled back.

--------------------------------------------------------------------------
SQL ENGINES (postgres / mysql / mssql / sqlite) - five layers
--------------------------------------------------------------------------
 1. Comments are stripped (`--`, `#`, `/* */`) and the remaining text must
    START with SELECT or WITH. Anything else is rejected outright, so an
    `INSERT`, a `CALL`, a `SET`, a `BEGIN` block, etc. never even reaches
    the keyword scan.
 2. No blocklisted keyword may appear anywhere as a STANDALONE TOKEN
    (word-boundary regex, not substring matching - a column called
    `updated_at` or `created_at` must not be falsely blocked, and it isn't:
    `\bUPDATE\b` cannot match inside `updated_at`).
 3. Only one statement: a `;` followed by anything other than whitespace
    is rejected, so `SELECT 1; DROP TABLE users` can't sneak a second
    statement past the prefix check.
 4. Defense in depth at execution time (in the adapters, not here): the
    query runs inside an explicit transaction that is ALWAYS rolled back,
    never committed - plus `SET TRANSACTION READ ONLY` on Postgres.
 5. A row cap and a statement timeout, both engine-correct (see
    `wrap_with_row_limit` below and each adapter's timeout handling).

String literals and quoted identifiers are masked before checks 2 and 3
run, so `SELECT 'please delete everything'` is not rejected for containing
the word DELETE inside a string, and a column that genuinely has to be
quoted (`"order"`, `` `update` ``, `[grant]`) doesn't trip the blocklist
either. The text that actually gets executed is the comment-stripped
original, never the masked form.

Known, deliberate false positive: `REPLACE` is on the blocklist because
`REPLACE INTO` is a write on MySQL/SQLite - which also rejects the
perfectly innocent `REPLACE(col, 'a', 'b')` string function. Erring
toward refusing a legitimate query rather than risking a write is the
right trade for this product; see the README's per-engine caveats.

--------------------------------------------------------------------------
MONGODB - an operation whitelist instead of SQL parsing
--------------------------------------------------------------------------
There is no SQL to parse, so the model is asked for a JSON operation spec
(`{"operation": "find", "collection": "orders", "filter": {...}}`) and
`validate_mongo_operation()` whitelists the four read operations
(find/aggregate/count/distinct), blocklists the pipeline stages that write
or execute server-side JavaScript (`$out`, `$merge`, `$function`,
`$accumulator`, `$where`), and normalizes the row cap in. Because a
`$where`/`$function` is equally dangerous inside a plain `find` filter, the
blocklist is applied recursively to filters too, not just to pipeline
stage keys.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple, Union

from app.engine.db_adapters.errors import NotReadOnlyError, QueryExecutionError

# --- SQL: static guard -----------------------------------------------------

# Only these two openers can begin a read-only statement. (`TABLE x` and
# `VALUES (...)` are also technically read-only on some engines, but they
# are not worth widening the surface for.)
_ALLOWED_PREFIX_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)

BLOCKED_KEYWORDS: Tuple[str, ...] = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "EXEC",
    "EXECUTE",
    "CALL",
    "MERGE",
    "REPLACE",
    "ATTACH",
    "DETACH",
    "PRAGMA",
)

_BLOCKED_RE = re.compile(r"\b(" + "|".join(BLOCKED_KEYWORDS) + r")\b", re.IGNORECASE)

_ORDER_BY_RE = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)

# Engines whose row cap is expressed as a trailing LIMIT. MSSQL is the
# odd one out and is handled separately - see wrap_with_row_limit().
_LIMIT_ENGINES = frozenset({"postgres", "mysql", "sqlite"})

_IDENTIFIER_QUOTES = {'"': '"', "`": "`", "[": "]"}


def _scan(sql: str) -> Tuple[str, str]:
    """Single-pass scan returning `(stripped, masked)`.

    `stripped` is `sql` with every comment replaced by a single space -
    this is what gets executed.
    `masked` is `stripped` with the *contents* of string literals and
    quoted identifiers replaced by a placeholder - this is what the
    blocklist and multi-statement checks run against, so neither can be
    fooled (or falsely tripped) by text inside quotes.

    Handles: `--` line comments, `#` line comments (MySQL), `/* */` block
    comments (including unterminated ones, treated as comment-to-end),
    single-quoted strings with both `''` and backslash escapes, and
    `"`/`` ` ``/`[ ]` quoted identifiers.
    """
    stripped: List[str] = []
    masked: List[str] = []
    i = 0
    n = len(sql)

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        # -- line comment / # line comment
        if (ch == "-" and nxt == "-") or ch == "#":
            end = sql.find("\n", i)
            end = n if end == -1 else end
            stripped.append(" ")
            masked.append(" ")
            i = end
            continue

        # /* block comment */  (unterminated -> rest of string is comment)
        if ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            stripped.append(" ")
            masked.append(" ")
            i = n if end == -1 else end + 2
            continue

        # 'string literal'
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
            stripped.append(sql[i:end])
            masked.append("'_str_'")
            i = end
            continue

        # "quoted identifier" / `quoted identifier` / [quoted identifier]
        if ch in _IDENTIFIER_QUOTES:
            closing = _IDENTIFIER_QUOTES[ch]
            end = sql.find(closing, i + 1)
            end = n if end == -1 else end + 1
            stripped.append(sql[i:end])
            masked.append("_ident_")
            i = end
            continue

        stripped.append(ch)
        masked.append(ch)
        i += 1

    return "".join(stripped), "".join(masked)


def strip_sql_comments(sql: str) -> str:
    """Public helper: `sql` with comments removed (nothing else changed)."""
    return _scan(sql)[0]


def assert_read_only_sql(sql: str) -> str:
    """Run every static check and return the cleaned SQL that should be
    executed (comments stripped, surrounding whitespace and any trailing
    semicolon removed).

    Raises NotReadOnlyError - with a message written for a human, since it
    is surfaced to the user through the assistant - on any violation.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise NotReadOnlyError("The query was empty, so there was nothing to run.")

    stripped, masked = _scan(sql)
    cleaned = stripped.strip()
    masked_clean = masked.strip()

    if not cleaned:
        raise NotReadOnlyError(
            "The query contained nothing but comments, so there was nothing to run."
        )

    # (1) must start with SELECT or WITH
    if not _ALLOWED_PREFIX_RE.match(masked_clean):
        first_word = (masked_clean.split() or ["(nothing)"])[0].strip("(;")
        raise NotReadOnlyError(
            "Only read-only queries are allowed: a query has to start with "
            f"SELECT or WITH, but this one started with '{first_word}'."
        )

    # (2) no blocklisted keyword as a standalone token
    blocked = _BLOCKED_RE.search(masked_clean)
    if blocked:
        raise NotReadOnlyError(
            f"Only read-only queries are allowed: '{blocked.group(1).upper()}' "
            "is not permitted anywhere in a query."
        )

    # (3) exactly one statement
    semicolon = masked_clean.find(";")
    if semicolon != -1 and masked_clean[semicolon + 1 :].strip():
        raise NotReadOnlyError(
            "Only one statement can be run at a time, and this query contained "
            "more than one (they were separated by a semicolon)."
        )

    return cleaned.rstrip().rstrip(";").rstrip()


def wrap_with_row_limit(sql: str, max_rows: int, engine: str) -> Optional[str]:
    """Return `sql` wrapped so the database itself caps the result set, or
    `None` when this query/engine combination cannot be safely wrapped (the
    caller must then execute it unwrapped and truncate in Python - every
    adapter does this as its fallback).

    Dialect differences that matter here:

      - postgres / mysql / sqlite: a trailing LIMIT on a derived table
        works, and all three accept a `WITH ...` CTE inside a derived
        table, so `SELECT * FROM (<query>) AS _sub LIMIT n` is always
        valid.
      - **mssql has no LIMIT at all** - the row cap is `SELECT TOP (n)`
        *before* the select list, so the wrapper is
        `SELECT TOP (n) * FROM (<query>) AS _sub`. Two further T-SQL rules
        make some queries unwrappable, and this function refuses to wrap
        them rather than generating SQL Server will reject:
          * a derived table may not contain a `WITH` CTE at all, so a query
            starting with WITH is returned unwrapped;
          * a derived table may not contain `ORDER BY` unless it also has
            its own TOP/OFFSET, so a query containing ORDER BY is returned
            unwrapped too.
        (`OFFSET 0 ROWS FETCH NEXT n ROWS ONLY` would handle the ORDER BY
        case but requires an ORDER BY to exist, which is exactly the
        opposite constraint - so Python-side truncation is the simpler
        correct answer for both.)
    """
    max_rows = max(1, int(max_rows))
    _, masked = _scan(sql)
    masked_clean = masked.strip()

    if engine == "mssql":
        if re.match(r"^\s*WITH\b", masked_clean, re.IGNORECASE):
            return None
        if _ORDER_BY_RE.search(masked_clean):
            return None
        return f"SELECT TOP ({max_rows}) * FROM (\n{sql}\n) AS _sub"

    if engine in _LIMIT_ENGINES:
        return f"SELECT * FROM (\n{sql}\n) AS _sub LIMIT {max_rows}"

    return None


# --- MongoDB: operation whitelist ------------------------------------------

ALLOWED_MONGO_OPERATIONS = frozenset({"find", "aggregate", "count", "distinct"})

# Stages/operators that write, or execute arbitrary server-side JavaScript.
# $where is included deliberately: it is a read operator, but it runs JS on
# the server, which is not something a model-authored query gets to do.
BLOCKED_MONGO_OPERATORS = frozenset({"$out", "$merge", "$function", "$accumulator", "$where"})


def _reject_blocked_operators(node: Any, where: str) -> None:
    """Recursively refuse a blocklisted operator anywhere inside a filter,
    projection or pipeline stage - not just at the top level, since
    `{"a": {"$where": "..."}}` is just as dangerous as a top-level one."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.lower() in BLOCKED_MONGO_OPERATORS:
                raise NotReadOnlyError(
                    f"'{key}' is not permitted in a query ({where}) - it can write "
                    "data or run server-side JavaScript."
                )
            _reject_blocked_operators(value, where)
    elif isinstance(node, list):
        for item in node:
            _reject_blocked_operators(item, where)


def validate_mongo_operation(
    query: Union[str, Dict[str, Any]], max_rows: int
) -> Dict[str, Any]:
    """Parse and validate a MongoDB operation spec, returning a normalized
    dict the adapter can execute directly.

    Accepted shapes (anything else raises):

        {"operation": "find",      "collection": "orders",
         "filter": {...}, "projection": {...}, "sort": {...},
         "skip": 0, "limit": 50}
        {"operation": "aggregate", "collection": "orders", "pipeline": [...]}
        {"operation": "count",     "collection": "orders", "filter": {...}}
        {"operation": "distinct",  "collection": "orders", "field": "status",
         "filter": {...}}

    Raises QueryExecutionError for a malformed request (bad JSON, missing
    collection/field) and NotReadOnlyError for a request that is
    well-formed but not allowed (an unknown operation, a blocklisted
    operator) - the two are distinguished because only the latter means
    "you tried to do something you're not permitted to do".

    The returned `limit` is always <= max_rows, and for `aggregate` a
    `$limit` stage is appended unless the pipeline already ends up capped
    at or below max_rows.
    """
    max_rows = max(1, int(max_rows))

    if isinstance(query, str):
        try:
            spec = json.loads(query)
        except (json.JSONDecodeError, TypeError) as exc:
            raise QueryExecutionError(
                "A MongoDB query has to be a JSON object describing the "
                'operation, e.g. {"operation": "find", "collection": "orders", '
                '"filter": {}} - this one was not valid JSON: ' + str(exc)
            ) from exc
    else:
        spec = query

    if not isinstance(spec, dict):
        raise QueryExecutionError(
            "A MongoDB query has to be a JSON object with an \"operation\" and a "
            '"collection" field.'
        )

    operation = spec.get("operation")
    if not isinstance(operation, str) or operation.strip().lower() not in ALLOWED_MONGO_OPERATIONS:
        raise NotReadOnlyError(
            "Only read operations are allowed on MongoDB "
            f"({', '.join(sorted(ALLOWED_MONGO_OPERATIONS))}) - "
            f"'{operation}' is not one of them."
        )
    operation = operation.strip().lower()

    collection = spec.get("collection")
    if not isinstance(collection, str) or not collection.strip():
        raise QueryExecutionError(
            'A MongoDB query needs a "collection" name (a non-empty string).'
        )
    collection = collection.strip()

    normalized: Dict[str, Any] = {"operation": operation, "collection": collection}

    if operation == "aggregate":
        pipeline = spec.get("pipeline")
        if not isinstance(pipeline, list):
            raise QueryExecutionError(
                'An aggregate needs a "pipeline" array of stages.'
            )
        for stage in pipeline:
            if not isinstance(stage, dict):
                raise QueryExecutionError(
                    "Every aggregation pipeline stage has to be a JSON object."
                )
            for key in stage:
                if isinstance(key, str) and key.lower() in BLOCKED_MONGO_OPERATORS:
                    raise NotReadOnlyError(
                        f"The aggregation stage '{key}' is not permitted - it can "
                        "write data or run server-side JavaScript."
                    )
            _reject_blocked_operators(stage, "aggregation pipeline")

        capped = _pipeline_is_capped(pipeline, max_rows)
        normalized["pipeline"] = list(pipeline) if capped else [*pipeline, {"$limit": max_rows}]
        normalized["limit"] = max_rows
        return normalized

    query_filter = spec.get("filter") or {}
    if not isinstance(query_filter, dict):
        raise QueryExecutionError('"filter" has to be a JSON object.')
    _reject_blocked_operators(query_filter, "filter")
    normalized["filter"] = query_filter

    if operation == "distinct":
        field = spec.get("field") or spec.get("key")
        if not isinstance(field, str) or not field.strip():
            raise QueryExecutionError(
                'A distinct needs a "field" name (a non-empty string).'
            )
        normalized["field"] = field.strip()
        normalized["limit"] = max_rows
        return normalized

    if operation == "count":
        normalized["limit"] = max_rows
        return normalized

    # find
    projection = spec.get("projection")
    if projection is not None and not isinstance(projection, dict):
        raise QueryExecutionError('"projection" has to be a JSON object.')
    if projection:
        _reject_blocked_operators(projection, "projection")
    normalized["projection"] = projection or None

    sort = spec.get("sort")
    if sort is not None and not isinstance(sort, dict):
        raise QueryExecutionError('"sort" has to be a JSON object, e.g. {"total": -1}.')
    normalized["sort"] = sort or None

    skip = spec.get("skip") or 0
    try:
        normalized["skip"] = max(0, int(skip))
    except (TypeError, ValueError):
        raise QueryExecutionError('"skip" has to be a whole number.') from None

    requested_limit = spec.get("limit")
    try:
        requested = int(requested_limit) if requested_limit is not None else max_rows
    except (TypeError, ValueError):
        raise QueryExecutionError('"limit" has to be a whole number.') from None
    normalized["limit"] = max(1, min(requested, max_rows))

    return normalized


def _pipeline_is_capped(pipeline: List[Dict[str, Any]], max_rows: int) -> bool:
    """True only if the pipeline's FINAL stage already caps output at or
    below `max_rows`.

    Deliberately strict about position: an earlier `$limit` says nothing
    about the output size, because a later `$unwind`/`$lookup` can multiply
    rows back out again. When in doubt, the caller appends its own trailing
    `$limit`, which is always safe."""
    if not pipeline:
        return False
    last = pipeline[-1]
    if not isinstance(last, dict) or "$limit" not in last:
        return False
    try:
        return int(last["$limit"]) <= max_rows
    except (TypeError, ValueError):
        return False
