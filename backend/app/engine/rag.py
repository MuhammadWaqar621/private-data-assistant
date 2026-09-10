"""
The agentic text-to-query engine.

`stream_agentic_reply()` is the single entrypoint app/api/messages.py
calls. The model is given two real tools and decides for itself, per
message, whether the question needs data at all - there is no Python
branching between canned prompts, and no keyword detection of "is this a
greeting". `schema_context` (app/engine/schema_rag.py) and
`example_context` (app/engine/example_rag.py) are both retrieved by the
caller from Qdrant BEFORE this function is called - this module never
queries Qdrant itself, only assembles what it's handed into the prompt.

  - `run_query(query)`   - one string argument. For a SQL connection that
                           string is SQL the model wrote; for a MongoDB
                           connection it is the JSON operation spec
                           described in mongodb_adapter.py. The system
                           prompt tells the model which, based on the
                           connection's engine.
  - `render_chart(chart_type, title, x_field, y_field)` - **no data
                           arguments at all**. It charts the rows returned
                           by the most recent `run_query` in this same
                           turn, which this module holds in a local
                           variable. The model therefore cannot invent
                           chart data: there is no parameter through which
                           to supply any.

--------------------------------------------------------------------------
WHY THIS MODULE DOESN'T RUN THE QUERY ITSELF
--------------------------------------------------------------------------
Executing a query needs a database driver and decrypted credentials, which
would drag app.core.crypto (and therefore the whole app) into app/engine/
and break the isolation contract in app/engine/__init__.py.

So `stream_agentic_reply()` is an `AsyncGenerator[dict, dict]`: when the
model asks to run a query, this function YIELDS

    {"type": "tool_call", "name": "run_query", "query": "...",
     "user_id": ..., "connection_id": ..., "engine": ...}

and suspends there until the caller resumes it via `asend(result)` with a
plain result dict:

    {"ok": True,  "columns": [...], "rows": [[...]], "row_count": 12,
     "truncated": False, "error": None}
    {"ok": False, "error": "The database rejected the query: ..."}

The `user_id`/`connection_id`/`engine` fields in the tool_call event are
ECHOED FROM THIS FUNCTION'S OWN ARGUMENTS - they never come from the model,
which has no parameter to set them. The caller supplies them from the
authenticated request and should use its own captured values; the echo
exists so the caller can assert they match (and so a log line about a
running query is self-describing).

The full protocol, with the caller's driving loop, is written out in
app/engine/__init__.py. Read that before changing either side.
"""

import json
from typing import Any, AsyncGenerator, Dict, List, Optional, Sequence, TypedDict

from openai import RateLimitError

from app.engine.llm_provider import get_active_chat_provider, get_chat_model_chain

# One completion per round. Three is exactly enough for the deepest useful
# path - run_query, then render_chart, then the final written answer - and
# the last round is issued with NO tools attached, which guarantees the
# loop terminates with prose instead of another tool request.
MAX_TOOL_ROUNDS = 3

# How many result rows are rendered into the tool message the model reads
# back. The row cap enforced against the database (MAX_QUERY_ROWS) governs
# what the CHART and the API response get; this smaller cap governs what is
# pasted into the prompt, so a 200-row result doesn't crowd out the schema
# context. The model is told the true total either way.
MAX_ROWS_IN_TOOL_RESULT = 50


class HistoryMessage(TypedDict):
    role: str  # "user" | "assistant"
    content: str


# --- Tool schemas ------------------------------------------------------------

RUN_QUERY_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_query",
        "description": (
            "Run a single READ-ONLY query against the user's connected "
            "database and get the resulting rows back. For SQL databases "
            "(PostgreSQL, MySQL/MariaDB, SQL Server, SQLite) pass the SQL "
            "text itself - it must be a single SELECT or WITH statement. "
            "For MongoDB pass a JSON operation spec instead, e.g. "
            '{"operation": "find", "collection": "orders", "filter": {}} '
            'or {"operation": "aggregate", "collection": "orders", '
            '"pipeline": [...]}. Writes of any kind are rejected before '
            "they reach the database."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The read-only query to run: SQL text for a SQL "
                        "database, or a JSON operation spec for MongoDB. "
                        "Use only tables/columns that appear in the schema "
                        "context you were given."
                    ),
                }
            },
            "required": ["query"],
        },
    },
}

RENDER_CHART_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "render_chart",
        "description": (
            "Render a chart of the rows returned by your most recent "
            "run_query call in this turn. You do NOT pass any data - the "
            "backend already holds those rows and will chart them. Just "
            "say which chart type to draw and which returned column "
            "supplies the category/x axis and which supplies the numeric "
            "value/y axis. Only call this after a successful run_query."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chart_type": {
                    "type": "string",
                    "enum": ["bar", "line", "pie"],
                    "description": (
                        "bar for comparing categories, line for a trend "
                        "over time, pie for parts of a whole."
                    ),
                },
                "title": {"type": "string", "description": "A short chart title."},
                "x_field": {
                    "type": "string",
                    "description": (
                        "Name of the column from the query result to use as "
                        "the category/x axis - it must be one of the "
                        "returned column names, spelled exactly."
                    ),
                },
                "y_field": {
                    "type": "string",
                    "description": (
                        "Name of the numeric column from the query result to "
                        "plot - it must be one of the returned column names, "
                        "spelled exactly."
                    ),
                },
            },
            "required": ["chart_type", "title", "x_field", "y_field"],
        },
    },
}

TOOLS: List[Dict[str, Any]] = [RUN_QUERY_TOOL, RENDER_CHART_TOOL]


# --- Prompting ---------------------------------------------------------------

AGENT_SYSTEM_PROMPT = (
    "You are Private Data Assistant. Users connect their OWN databases "
    "(PostgreSQL, MySQL/MariaDB, SQL Server, SQLite, MongoDB) and ask "
    "questions about their live data in plain language. You answer by "
    "writing a read-only query, running it with the run_query tool, and "
    "explaining the result.\n\n"
    "WHEN NOT TO USE A TOOL. Greetings, small talk, thanks, and questions "
    "about you or how this product works ('what is this', 'what can you "
    "do', 'which databases do you support') are answered directly, in your "
    "own words, with no tool call at all. Never run a query just to be "
    "polite.\n\n"
    "WHEN TO USE run_query. Any question about the user's actual data - "
    "counts, totals, lists, comparisons, trends, 'do we have', 'who', "
    "'how many', 'show me' - MUST go through run_query first. Never answer "
    "a data question from assumption, from memory, or from the sample rows "
    "in the schema context: those samples exist to show you the shape and "
    "format of the data, never to be reported as the answer. If you are "
    "not sure whether a question is about their data, run the query.\n\n"
    "WRITING THE QUERY. Use only tables and columns that appear in the "
    "schema context you were given - if the context doesn't contain what "
    "the question needs, say so plainly and suggest what you would need, "
    "rather than guessing a table name. Prefer aggregate queries "
    "(COUNT/SUM/GROUP BY) over dumping raw rows. Always constrain the "
    "result to a sensible size. The query must be read-only: a single "
    "SELECT or WITH statement, with no INSERT/UPDATE/DELETE/DDL of any "
    "kind. Writes are rejected before they reach the database, so "
    "attempting one only wastes a turn.\n\n"
    "AFTER THE QUERY RUNS. Answer using ONLY the rows that came back. "
    "State the numbers as they are; don't round silently or invent rows "
    "that weren't returned. If the result was empty, say so - an empty "
    "result is a real answer ('no orders matched'), not a failure. If the "
    "result was truncated you will be told, and you should mention it.\n\n"
    "IF THE QUERY FAILS. You will get an error message back instead of "
    "rows. Apologize briefly, explain in plain language what went wrong "
    "(a column that doesn't exist, a query that wasn't read-only, a "
    "timeout), and either correct it and try once more or ask the user for "
    "what you're missing. Never paste a raw stack trace, and never blame "
    "the user.\n\n"
    "CHARTS. If the answer is naturally visual - a comparison across "
    "categories, a trend over time, a breakdown of a whole - you may call "
    "render_chart in the same turn, AFTER a successful run_query. It takes "
    "no data: it charts the rows that query returned, and x_field/y_field "
    "must be exact column names from that result. Still write the "
    "sentence-level answer as well; the chart supplements it.\n\n"
    "FORMAT. Reply in Markdown: **bold** for emphasis, bullet or numbered "
    "lists for multiple items, tables for tabular results, headings where "
    "they genuinely help. Don't force structure onto a one-line answer."
)

_NO_CONNECTION_PROMPT = (
    "\n\nIMPORTANT - NO DATABASE IS CONNECTED TO THIS CHAT. You have no "
    "tools available in this turn and no schema to work from. Answer "
    "greetings and questions about the product normally. For anything "
    "about the user's data, tell them plainly that they need to connect a "
    "database (or select one for this chat) first, and offer to help once "
    "they have. Do not invent data, and do not pretend to have run "
    "anything."
)

_DIALECT_HINTS = {
    "postgres": (
        "The connected database is **PostgreSQL**. Write standard "
        "PostgreSQL SQL. Identifiers are folded to lower case unless "
        'double-quoted, so quote them ("MyTable") only when the schema '
        "context shows mixed case. Use LIMIT for row caps."
    ),
    "mysql": (
        "The connected database is **MySQL/MariaDB**. Write MySQL SQL. "
        "Backticks quote identifiers. Use LIMIT for row caps."
    ),
    "mssql": (
        "The connected database is **Microsoft SQL Server**. Write T-SQL. "
        "There is NO LIMIT clause - use `SELECT TOP (n) ...` instead, and "
        "square brackets [like this] to quote identifiers. Date functions "
        "are DATEADD/DATEDIFF/GETDATE(), not INTERVAL/NOW()."
    ),
    "sqlite": (
        "The connected database is **SQLite**. Write SQLite SQL. Use LIMIT "
        "for row caps. Dates are usually stored as TEXT/INTEGER, so use "
        "date()/datetime()/strftime() rather than server date types."
    ),
    "mongodb": (
        "The connected database is **MongoDB**, so run_query does NOT take "
        "SQL. Pass a JSON object describing one read operation:\n"
        '  {"operation": "find", "collection": "orders", "filter": {...}, '
        '"projection": {"_id": 0}, "sort": {"total": -1}, "limit": 20}\n'
        '  {"operation": "aggregate", "collection": "orders", "pipeline": '
        "[...]}\n"
        '  {"operation": "count", "collection": "orders", "filter": {...}}\n'
        '  {"operation": "distinct", "collection": "orders", "field": '
        '"status"}\n'
        "Those four operations are the only ones permitted. $out, $merge, "
        "$function, $accumulator and $where are rejected. The 'columns' "
        "in the schema context are FIELDS INFERRED FROM A SAMPLE of "
        "documents, so a rarely-used field may be missing from it."
    ),
}


def build_system_prompt(engine_name: Optional[str], has_connection: bool) -> str:
    """The full system prompt for one turn: the base agent instructions,
    plus either the 'no database connected' addendum or the dialect hint
    for whichever engine this chat's connection uses."""
    if not has_connection:
        return AGENT_SYSTEM_PROMPT + _NO_CONNECTION_PROMPT

    hint = _DIALECT_HINTS.get((engine_name or "").strip().lower())
    if hint:
        return AGENT_SYSTEM_PROMPT + "\n\n" + hint
    return AGENT_SYSTEM_PROMPT


def _history_messages(history: Optional[Sequence[HistoryMessage]]) -> List[dict]:
    """Prior turns as plain {"role", "content"} dicts, dropping anything
    with an unrecognized role or empty content."""
    messages: List[dict] = []
    for turn in history or []:
        role = turn.get("role")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    return messages


def _example_context_message(example_context: str) -> Optional[dict]:
    """Few-shot worked examples (see app/engine/example_rag.py): past
    questions asked against this same connection, paired with the exact
    read-only query that answered them - either seeded from the schema's
    foreign-key relationships at registration time, or captured from a
    real successful run_query call. Omitted entirely when there's nothing
    relevant yet (a brand new connection with no FK relationships and no
    usage history), unlike the schema context's "none matched" case -
    examples are a helpful nudge, never something the model needs telling
    it's missing."""
    if not (example_context or "").strip():
        return None
    return {
        "role": "system",
        "content": (
            "SIMILAR PAST QUESTIONS - worked examples of the query style "
            "this database needs, from past questions asked against this "
            "same connection (some seeded from its foreign-key "
            "relationships, some from real prior turns). Use them as a "
            "pattern for table names, join syntax, and column naming when "
            "they're relevant to the current question - but always write a "
            "fresh query tailored to what's actually being asked now, and "
            "ignore any example that doesn't fit.\n\n" + example_context
        ),
    }


def _schema_context_message(schema_context: str, has_connection: bool) -> Optional[dict]:
    if not has_connection:
        return None
    if not (schema_context or "").strip():
        return {
            "role": "system",
            "content": (
                "SCHEMA CONTEXT: none of this database's tables matched this "
                "question (it may not be indexed yet, or it may be empty). "
                "Do not guess table or column names - tell the user you "
                "can't see a matching table and suggest re-indexing the "
                "connection or rephrasing."
            ),
        }
    return {
        "role": "system",
        "content": (
            "SCHEMA CONTEXT - the tables from the user's connected database "
            "that best match this question. Write your query against these "
            "and nothing else. Sample rows are shown to illustrate the "
            "format of each column; they are NOT the answer to any "
            "question.\n\n" + schema_context
        ),
    }


def format_query_result(result: Dict[str, Any]) -> str:
    """Render a run_query result into the text the model reads back as the
    tool message. Also used by tests to pin the shape."""
    if not result.get("ok"):
        return (
            "The query did not run. Error: "
            + str(result.get("error") or "unknown error")
            + "\nFix the query and try again, or explain the problem to the user."
        )

    columns: List[str] = list(result.get("columns") or [])
    rows: List[List[Any]] = list(result.get("rows") or [])
    if not rows:
        return "The query ran successfully and returned 0 rows."

    shown = rows[:MAX_ROWS_IN_TOOL_RESULT]
    lines = [
        "| " + " | ".join(str(column) for column in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in shown:
        cells = [
            ("" if value is None else str(value)).replace("|", "\\|").replace("\n", " ")[:120]
            for value in row
        ]
        lines.append("| " + " | ".join(cells) + " |")

    footer = f"\n\n({len(rows)} row(s) returned"
    if len(shown) < len(rows):
        footer += f", first {len(shown)} shown here"
    if result.get("truncated"):
        footer += "; the result hit the server's row cap, so there may be more"
    footer += ".)"
    return "\n".join(lines) + footer


# --- The agent loop ------------------------------------------------------------


async def stream_agentic_reply(
    user_id: int,
    connection_id: Optional[int],
    engine_name: Optional[str],
    chat_history: Optional[Sequence[HistoryMessage]],
    message: str,
    schema_context: str = "",
    example_context: str = "",
) -> AsyncGenerator[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Drive one assistant turn, yielding a discriminated stream of events.

    Must be driven with `asend()` (see the handshake described in
    app/engine/__init__.py) - `async for` works only for a turn that never
    runs a query, since it cannot send the result back in.

    Events yielded:
      {"type": "token",     "text": str}
      {"type": "tool_call", "name": "run_query", "query": str,
                            "user_id": int, "connection_id": int|None,
                            "engine": str|None}   <- caller must asend() a result
      {"type": "chart",     "chart_type","title","x_field","y_field",
                            "columns": [...], "rows": [[...]]}
      {"type": "done",      "content": str, "query_sql": str|None,
                            "chart_spec": dict|None}
      {"type": "error",     "message": str}

    `user_id` / `connection_id` / `engine_name` come from the authenticated
    request and are never influenced by the model - it has no tool
    parameter for any of them.
    """
    has_connection = connection_id is not None

    try:
        provider = get_active_chat_provider()  # caller must check ai_configured() first
    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": f"The AI provider is not available: {exc}"}
        return

    messages: List[dict] = [
        {"role": "system", "content": build_system_prompt(engine_name, has_connection)}
    ]
    schema_message = _schema_context_message(schema_context, has_connection)
    if schema_message is not None:
        messages.append(schema_message)
    example_message = _example_context_message(example_context) if has_connection else None
    if example_message is not None:
        messages.append(example_message)
    messages.extend(_history_messages(chat_history))
    messages.append({"role": "user", "content": message})

    full_text = ""
    query_sql: Optional[str] = None
    chart_spec: Optional[Dict[str, Any]] = None
    # The rows from the most recent successful run_query IN THIS TURN, held
    # here and nowhere else. render_chart reads them from this variable, so
    # the model can never supply chart data - see the module docstring.
    last_result: Optional[Dict[str, Any]] = None

    for round_index in range(MAX_TOOL_ROUNDS):
        is_final_round = round_index == MAX_TOOL_ROUNDS - 1
        # No tools on the last round (and none at all when no database is
        # connected): a structural guarantee that the loop ends with a
        # written answer rather than another tool request.
        tools = TOOLS if (has_connection and not is_final_round) else None

        request: Dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"

        stream = None
        last_rate_limit_exc: Optional[Exception] = None
        for model in await get_chat_model_chain(provider):
            request["model"] = model
            try:
                stream = await provider.client.chat.completions.create(**request)
                break
            except RateLimitError as exc:
                # This model's own quota is exhausted (Groq only - Azure
                # never returns more than one model to try, see
                # get_chat_model_chain) - try the next one in the chain
                # immediately, no tokens have been streamed yet.
                last_rate_limit_exc = exc
                continue
            except Exception as exc:  # noqa: BLE001 - a real failure, not a quota issue - stop here
                yield {"type": "error", "message": f"The AI provider failed: {exc}"}
                yield {
                    "type": "done",
                    "content": full_text,
                    "query_sql": query_sql,
                    "chart_spec": chart_spec,
                }
                return

        if stream is None:
            # Every model in the chain is rate-limited right now.
            yield {"type": "error", "message": f"The AI provider failed: {last_rate_limit_exc}"}
            yield {
                "type": "done",
                "content": full_text,
                "query_sql": query_sql,
                "chart_spec": chart_spec,
            }
            return

        round_text = ""
        tool_calls_acc: Dict[int, dict] = {}
        try:
            async for event in stream:
                if not event.choices:
                    continue
                delta = event.choices[0].delta
                if delta is None:
                    continue
                if delta.content:
                    round_text += delta.content
                    yield {"type": "token", "text": delta.content}
                if delta.tool_calls:
                    # The streaming API sends a function call's
                    # name/arguments in fragments across chunks, keyed by
                    # index - accumulate them the same way content deltas
                    # are accumulated.
                    for tool_call in delta.tool_calls:
                        entry = tool_calls_acc.setdefault(
                            tool_call.index, {"id": None, "name": None, "arguments": ""}
                        )
                        if tool_call.id:
                            entry["id"] = tool_call.id
                        if tool_call.function and tool_call.function.name:
                            entry["name"] = tool_call.function.name
                        if tool_call.function and tool_call.function.arguments:
                            entry["arguments"] += tool_call.function.arguments
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"The reply stream failed: {exc}"}
            break

        full_text += round_text

        if not tool_calls_acc:
            break

        messages.append(
            {
                "role": "assistant",
                "content": round_text or None,
                "tool_calls": [
                    {
                        "id": tool_calls_acc[index]["id"],
                        "type": "function",
                        "function": {
                            "name": tool_calls_acc[index]["name"] or "run_query",
                            "arguments": tool_calls_acc[index]["arguments"] or "{}",
                        },
                    }
                    for index in sorted(tool_calls_acc)
                ],
            }
        )

        for index in sorted(tool_calls_acc):
            entry = tool_calls_acc[index]
            name = entry["name"] or "run_query"
            try:
                arguments = json.loads(entry["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}

            if name == "run_query":
                query = str(arguments.get("query") or "").strip()
                if not query:
                    tool_content = (
                        "No query was supplied. Call run_query again with the "
                        "query text in the `query` argument."
                    )
                else:
                    query_sql = query
                    # --- the handshake: suspend here until the caller
                    # executes the query and sends the result back in.
                    result = yield {
                        "type": "tool_call",
                        "name": "run_query",
                        "query": query,
                        # Echoed from this function's own arguments, never
                        # from the model - see the module docstring.
                        "user_id": user_id,
                        "connection_id": connection_id,
                        "engine": engine_name,
                    }
                    if not isinstance(result, dict):
                        result = {
                            "ok": False,
                            "error": "The query could not be executed (no result was returned).",
                        }
                    if result.get("ok"):
                        last_result = result
                    tool_content = format_query_result(result)

            elif name == "render_chart":
                if not last_result or not last_result.get("rows"):
                    tool_content = (
                        "There are no query results to chart yet. Call "
                        "run_query first, then render_chart."
                    )
                else:
                    chart_type = str(arguments.get("chart_type") or "bar").lower()
                    if chart_type not in ("bar", "line", "pie"):
                        chart_type = "bar"
                    columns = list(last_result.get("columns") or [])
                    rows = list(last_result.get("rows") or [])
                    chart_spec = {
                        "chart_type": chart_type,
                        "title": str(arguments.get("title") or ""),
                        "x_field": str(arguments.get("x_field") or ""),
                        "y_field": str(arguments.get("y_field") or ""),
                        # The DATA is taken from the held query result, not
                        # from anything the model passed - render_chart's
                        # schema has no data parameter at all.
                        "columns": columns,
                        "rows": rows,
                    }
                    yield {"type": "chart", **chart_spec}
                    tool_content = (
                        f"Chart rendered for the user: a {chart_type} chart of "
                        f"{len(rows)} row(s). Now write the answer in words as well."
                    )

            else:
                tool_content = (
                    f"'{name}' is not a tool you have. The available tools are "
                    "run_query and render_chart."
                )

            messages.append(
                {"role": "tool", "tool_call_id": entry["id"], "content": tool_content}
            )

    yield {
        "type": "done",
        "content": full_text,
        "query_sql": query_sql,
        "chart_spec": chart_spec,
    }
