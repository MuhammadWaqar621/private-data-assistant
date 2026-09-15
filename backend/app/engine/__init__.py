"""
app.engine - the data/AI engine, kept fully independent of the rest of the
FastAPI application.

Isolation contract (do not violate this):
  - Nothing in this package imports from app.api, app.models (SQLAlchemy),
    or any auth code (app.core.security / app.core.crypto / app.api.deps).
  - Every function here takes plain arguments (ints, strings, plain
    dicts/dataclasses) and returns plain Python objects - never an ORM
    object, never a FastAPI Request/Response, never a `Settings` instance.
  - Configuration is read directly from environment variables (see
    engine/azure_client.py, engine/schema_rag.py) rather than through
    app.core.config.Settings, so this package has zero dependency on the
    rest of the app and can be imported/tested/reused in isolation (e.g.
    in a standalone script, a notebook, or a different service entirely).
    Per-request limits that ARE app policy (max rows, query timeout) are
    passed in as plain arguments by the caller instead of read here.
  - Credentials arrive already decrypted, as a plain
    `db_adapters.base.ConnectionInfo` - this package neither knows nor
    cares that they are stored encrypted (that is app/core/crypto.py's
    job).

The API layer (app/api/connections.py, app/api/messages.py) is the ONLY
code allowed to touch both the DB/auth stack and this package - it checks
auth/ownership, decrypts credentials, calls a plain engine function, and
persists the result.

Modules:
  - azure_client.py:  Azure OpenAI client construction (embeddings + chat)
  - groq_client.py:   Groq client construction (chat completions when selected)
  - llm_provider.py:  chat-provider selection (LLM_PROVIDER=groq|azure) -
                       embeddings are always Azure; only chat is selectable
  - db_adapters/:     one adapter per supported engine (postgres, mysql,
                       mssql, sqlite, mongodb) behind a common DBAdapter
                       interface: test_connection / introspect_schema /
                       execute_read_only. Read-only enforcement lives in
                       db_adapters/readonly.py (pure functions, stdlib only)
  - schema_rag.py:    schema chunking, embedding, pgvector storage/search,
                       with per-(user_id, connection_id) isolation enforced
                       unconditionally on every search
  - rag.py:           the agentic text-to-query loop (stream_agentic_reply)

--------------------------------------------------------------------------
THE rag.py <-> messages.py HANDSHAKE (read this before touching either)
--------------------------------------------------------------------------
`rag.py` must decide *whether* to run a query and *what* query to run, but
it must never import a database adapter or open a connection - otherwise
the engine's isolation contract collapses (it would need credentials, and
therefore the DB/crypto stack).

The chosen mechanism is a **generator-based handshake** on
`rag.stream_agentic_reply()`, which is an `AsyncGenerator[dict, dict]`:

  1. The caller drives it manually with `asend()` (NOT `async for`, which
     cannot send values back in):

         agen = stream_agentic_reply(...)
         to_send = None
         while True:
             try:
                 event = await agen.asend(to_send)
             except StopAsyncIteration:
                 break
             to_send = None
             if event["type"] == "tool_call":       # {"name": "run_query", "query": ...}
                 to_send = <execute it for real>   # a plain result dict
             else:
                 <forward event to the client as SSE>

  2. When the model asks to run a query, rag.py yields
     `{"type": "tool_call", "name": "run_query", "query": "<text>"}` and
     *blocks on that yield* until the caller sends back a plain result
     dict: `{"ok": bool, "columns": [...], "rows": [[...]], "row_count":
     int, "truncated": bool, "error": str | None}`.
  3. rag.py renders that result into the `role="tool"` message the model
     sees next, and keeps the rows in a local variable for the duration of
     the turn. If the model then calls `render_chart`, rag.py emits a
     `{"type": "chart", ...}` event carrying THOSE held rows - the chart
     tool schema has no data parameters at all, so the model physically
     cannot invent chart data.

Everything the model can influence is free text (`query`) or an enum/label
(`chart_type`, `title`, `x_field`, `y_field`). `user_id`,
`connection_id` and `engine_name` are captured by app/api/messages.py from
the authenticated request before the generator ever runs, and are used
only on the caller's side of the handshake - the model has no parameter
through which to name a different user, connection, or database.
"""
