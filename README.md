# Private Data Assistant

Ask questions about **your own databases** in plain language.

You register a connection to a database you already have - PostgreSQL,
MySQL/MariaDB, SQL Server, SQLite, or MongoDB - and then just ask: *"how
many orders shipped late last month?"*, *"what's revenue by country?"*,
*"chart signups by plan"*. The assistant reads your **schema** (not your
data), works out which tables matter, writes a **read-only** query, runs it
against your live database, and answers from the rows that come back -
streaming the reply token by token, optionally with a chart.

General-purpose tools like ChatGPT or Claude have never seen your schema
and cannot reach your database. Private Data Assistant exists to close that
gap without your data ever being copied anywhere: the only thing that
leaves your database is the result of the specific query you asked for, and
the only thing this application stores is your schema (as embeddings) plus
the chat transcript.

> **Status: complete end to end.** Connection registration, schema
> introspection + embedding, retrieval, the agentic text-to-query loop,
> read-only enforcement per engine, SSE streaming, charts, auth,
> migrations and the test suite are implemented in `backend/`; the
> React + TypeScript + Vite + Tailwind UI is in `frontend/`.
> `docker-compose up` brings up Postgres, Qdrant, the API on
> [:8000](http://localhost:8000/docs) and the UI on
> [:4173](http://localhost:4173). See "Getting started" for the full
> walkthrough - in the UI, and with `curl` for the same steps.

This is the sibling of
[private-document-assistant](../private-document-assistant), and
deliberately reuses its architecture (FastAPI, SQLAlchemy + Alembic, JWT
auth, Postgres for metadata, Qdrant for vectors, SSE streaming, an isolated
`engine/` package, provider-selectable LLM, a `GET /api/config/status`
gating pattern). The domain is what changed: **schema-RAG + text-to-query
against live databases** instead of document RAG.

---

## Table of contents

1. [Architecture](#architecture)
2. [How a question becomes an answer](#how-a-question-becomes-an-answer) - the full flow, step by step
3. [Read-only enforcement, per engine](#read-only-enforcement-per-engine)
4. [Credential handling](#credential-handling)
5. [The `engine/` isolation contract and the generator handshake](#the-engine-isolation-contract-and-the-generator-handshake)
6. [Getting started](#getting-started)
7. [API reference](#api-reference)
8. [Environment variables](#environment-variables)
9. [Running tests](#running-tests)
10. [Roadmap / known tradeoffs](#roadmap--known-tradeoffs)

---

## Architecture

```mermaid
flowchart TB
    subgraph Client
        FE["Frontend · frontend/<br/>React 18 + Vite + TypeScript + Tailwind<br/>chat shell · per-chat database selector<br/>manual SSE reader · Chart.js"]
    end

    subgraph Backend["Backend - FastAPI"]
        API["API layer · app/api/*.py<br/>auth · ownership checks · credential encrypt/decrypt<br/>DB persistence · SSE streaming<br/>the ONLY place a query is executed"]
        subgraph ENGINE["engine/ package - zero imports from api/models/auth"]
            ADAPT["db_adapters/<br/>postgres · mysql · mssql · sqlite · mongodb<br/>test_connection · introspect_schema · execute_read_only<br/>readonly.py: the guard (pure functions)"]
            SRAG["schema_rag.py<br/>chunk per table · embed · upsert<br/>search filtered by user_id AND connection_id"]
            RAG["rag.py<br/>agentic loop · run_query + render_chart tools<br/>yields tool_call, caller executes it"]
        end
    end

    PG[("Postgres - THIS app's metadata<br/>users · chats · messages<br/>database_connections<br/>(no user business data, ever)")]
    QD[("Qdrant<br/>one vector per TABLE SCHEMA<br/>every point tagged<br/>user_id + connection_id")]
    USERDB[("The USER's own database<br/>Postgres / MySQL / MSSQL /<br/>SQLite / MongoDB<br/>READ-ONLY, never modified")]
    AZ["Azure OpenAI<br/>embeddings (always) +<br/>chat (if LLM_PROVIDER=azure)"]
    GQ["Groq<br/>chat completions<br/>(if LLM_PROVIDER=groq, default)"]

    FE -- "HTTP /api/* + SSE" --> API
    API -- "plain args in, plain data out" --> ENGINE
    API -- "SQLAlchemy" --> PG
    API -- "executes the model's query<br/>via get_adapter(engine)" --> ADAPT
    ADAPT -- "SELECT only, in a transaction<br/>that is always rolled back" --> USERDB
    SRAG -- "upsert / search,<br/>always filtered by<br/>user_id AND connection_id" --> QD
    SRAG -- "embeddings" --> AZ
    RAG -- "streamed chat completions" --> GQ
    RAG -- "streamed chat completions" --> AZ
```

Three storage systems, three completely different jobs:

| | What it holds | Who writes it |
|---|---|---|
| **Postgres** (this app's) | users, chats, messages, and the *registration record* for each database you connect (host, port, database name, username, **encrypted** password, indexing status) | this app |
| **Qdrant** | one embedded chunk per **table**, tagged `user_id` + `connection_id` + `engine`. Column names, types, keys, and up to 5 sample rows | this app |
| **Your database** | your actual data | **you** - this app only ever reads it |

The only copy of your data that this application ever holds is (a) up to
five sample rows per table, embedded into the schema chunk so the model can
see what a date or a status value actually looks like, and (b) the rows of
a chart you asked it to draw, persisted on the message so reloading the
history re-renders it.

### Components

- **Backend** - Python 3.11+, FastAPI, SQLAlchemy + Alembic, Postgres for
  its own relational data, Qdrant for schema vectors. Embeddings always go
  through **Azure OpenAI** (`openai.AzureOpenAI`); **chat completions** are
  provider-selectable via `LLM_PROVIDER` (`groq`, the default, or `azure`)
  - Groq's API is OpenAI-compatible, so the same `openai` package serves
  both.
- **`app/engine/`** - the data/AI engine (adapters, schema-RAG, the agentic
  loop, provider selection), a self-contained package with **zero imports**
  from `app/api`, `app/models`, or auth/crypto code. See
  [the isolation contract](#the-engine-isolation-contract-and-the-generator-handshake).
- **`app/engine/db_adapters/`** - one adapter per engine behind a common
  `DBAdapter` interface, with `get_adapter(engine)` as the single dispatch
  point. `readonly.py` (pure functions, standard library only) holds the
  read-only guard.
- **Auth** - email/password only (JWT access + refresh tokens,
  `passlib`/bcrypt), plus forgot/reset password via SMTP (`aiosmtplib`).
- **Frontend** - React 18 + TypeScript + Vite 6 + Tailwind 3, with
  `react-router-dom` for routing, `chart.js` + `react-chartjs-2` for
  charts, `react-markdown` + `remark-gfm` for assistant replies, and
  `lucide-react` for icons. Same stack, structure and visual language as
  the sibling private-document-assistant frontend. See below.

### Frontend layout

```
frontend/src/
  App.tsx        /  /login  /signup  /forgot-password  /reset-password  /app
  lib/           config.ts (apiUrl) · auth.ts (token storage)
                 api.ts (fetch wrapper: Bearer header, 401 -> refresh -> retry;
                         get/post/patch/del/uploadFile)
                 chatStream.ts (the SSE consumer) · chart.ts (chart-spec validation)
                 types.ts · engines.ts · theme.tsx · useConfigStatus.ts
                 passwordPolicy.ts · avatar.ts
  components/    AuthLayout · ProtectedRoute · ThemeToggle · MarkdownMessage
                 ChartAdapter · QueryResultTable · ConnectionsModal
  pages/         HomePage · LoginPage · SignupPage · ForgotPasswordPage
                 ResetPasswordPage · AppShellPage
```

Three decisions that are load-bearing rather than cosmetic:

- **`lib/chatStream.ts` reads the SSE stream with `fetch` + a
  `ReadableStream` reader, not `EventSource`.** `EventSource` only issues
  GETs and cannot attach an `Authorization: Bearer` header, and this
  endpoint is bearer-protected like every other one. It splits events on
  `\n\n` and dispatches `token` / `query` / `query_result` / `chart` /
  `done` / `error`. A `query_result` with `ok: false` is rendered as a live
  status line and the reader **keeps going** - the model is handed that
  error and explains or retries in its next tokens.
- **`components/ChartAdapter.tsx` builds Chart.js's `{labels, datasets}`
  itself** from the `{chart_type, title, x_field, y_field, columns, rows}`
  the backend sends. Because the real rows are therefore already on the
  client, every chart gets a free "show the data" disclosure
  (`QueryResultTable`), and a chart whose `x_field`/`y_field` don't match
  any column degrades to that table instead of throwing.
- **The chat header's database selector PATCHes `connection_id` alone.**
  `PATCH /api/chats/{id}` keys off key *presence*, so sending only
  `{"connection_id": N}` (or `null`) leaves the title untouched, and the
  inline rename sends only `{"title": ...}`.

`frontend/README.md` has the full file-by-file map.

---

## How a question becomes an answer

This is the part worth reading closely. Two phases: registering a database
(once), and asking a question (every time).

### Phase 1 - register a connection (once per database)

```
POST /api/connections            ->  row created, status=pending
   |
   |-- status=indexing
   |
   |-- adapter.test_connection(conn)          real connect + SELECT 1
   |        fail -> status=failed + error_message, HTTP 201 with that row
   |
   |-- adapter.introspect_schema(conn)        every table/collection:
   |        columns (name, type, nullable, PK, FK -> target)
   |        + up to 5 real sample rows
   |        fail -> status=failed + error_message
   |
   |-- schema_rag.build_table_documents(tables)
   |        ONE text chunk per table, rendered as readable Markdown
   |
   |-- azure embeddings  ->  one vector per chunk
   |
   |-- schema_rag.index_connection_schema(...)
   |        upsert into Qdrant, EVERY point tagged
   |        {user_id, connection_id, engine, table_name, text}
   |        (old points for this connection are deleted first)
   |
   `-- status=ready, schema_indexed_at=now
```

**1. You POST the connection details.** Network engines
(`postgres`/`mysql`/`mssql`/`mongodb`) send JSON; SQLite uploads the
database file to `POST /api/connections/sqlite` instead, which stores it at
`storage/{user_id}/{connection_id}/database.sqlite`. The password is
encrypted with Fernet *before* the row is written -
[see below](#credential-handling).

**2. The connection is tested for real.** `test_connection()` opens an
actual connection and runs `SELECT 1` (or a Mongo `ping` against your
database, not `admin` - so credentials scoped to one database work). A
failure is recorded on the row, not raised: the request returns 201 with
`status: "failed"` and a message like *"Could not connect: password
authentication failed for user 'reader'"*.

**3. The schema is introspected.** For SQL engines this is SQLAlchemy's
`Inspector` - table names, then per table the columns with their types,
nullability, primary key membership, and foreign keys resolved to
`other_table.other_column` - plus `SELECT * ... LIMIT 5` for samples.
MongoDB has no declared schema, so its adapter samples ~20 documents per
collection and infers a field schema from them (`total: double | null`,
`tags: array<string>`), plus 5 raw sample documents.

**4. One chunk per table is built and embedded.** Not raw DDL - readable
text, because that is what an embedding model matches a natural-language
question against:

```
Table: orders
Columns:
  - id (INTEGER) [primary key, not null]
  - customer_id (INTEGER) [foreign key -> customers.id, nullable]
  - status (VARCHAR) [not null]
  - total (NUMERIC(10, 2)) [nullable]

Sample rows (up to 5):
| id | customer_id | status | total |
| --- | --- | --- | --- |
| 1 | 7 | shipped | 120.00 |
| 2 | 7 | pending | 85.50 |
```

The sample rows earn their place: they show the model that `status` is
`'shipped'` and not `'SHIPPED'` or `2`, that `placed_at` is a date and not
an epoch integer, that money is in units and not cents. That is most of the
difference between a query that runs and one that returns zero rows.

**5. Everything is upserted into Qdrant**, each point carrying `user_id`,
`connection_id`, `engine` and `table_name` in its payload. The point id is
a deterministic UUID of `(user_id, connection_id, table_name)`, so
re-indexing overwrites rather than accumulating, and two users whose
connection #5 both have an `orders` table get two distinct points.

The row flips to `status: "ready"` with `schema_indexed_at` set. All of
this happens **synchronously, inside the request** - a deliberate tradeoff,
[see the roadmap](#roadmap--known-tradeoffs).

### Phase 2 - ask a question (every message)

```
POST /api/chats/{id}/messages  {"content": "revenue by country?"}
   |
   |-- load the chat (404 unless it's yours)
   |-- load its connection_id -> the DatabaseConnection (404 unless yours)
   |-- CAPTURE user_id, connection_id, engine_name, and the decrypted
   |   ConnectionInfo as PLAIN VALUES, before any streaming starts
   |
   |-- schema_rag.retrieve_relevant_schema(user_id, connection_id, question)
   |        embed the question (Azure)
   |        search Qdrant with must-filters on user_id AND connection_id
   |        -> the top-8 matching table chunks, joined into one string
   |        (Qdrant only - your database is NOT touched here)
   |
   |-- persist the user Message; auto-title the chat if it's the first
   |
   `-- rag.stream_agentic_reply(...)   <- an async generator
            |
            round 1: streamed completion with tools=[run_query, render_chart]
            |   * a greeting? -> it just answers. Tokens stream out. Done.
            |   * a data question? -> it emits a run_query tool call
            |
            |   yields {"type": "tool_call", "query": "SELECT ..."}
            |     ^ SUSPENDS HERE. app/api/messages.py:
            |         get_adapter(engine_name).execute_read_only(
            |             connection_info, query, MAX_QUERY_ROWS,
            |             QUERY_TIMEOUT_SECONDS)
            |       and resumes the generator with the real result via asend()
            |
            round 2: the rows are appended as a role="tool" message and a
            |        second streamed completion writes the answer.
            |        It may also call render_chart, which charts the rows
            |        the backend is holding - the tool takes no data.
            |
            round 3: issued with NO tools attached, guaranteeing the loop
                     ends in prose rather than another tool request.
   |
   `-- SSE out:  event: token       {"content": "..."}     (many)
                 event: query       {"query": "SELECT ..."}
                 event: query_result{"ok": true, "row_count": 12}
                 event: chart       {chart_type, title, x_field, y_field,
                                     columns, rows}
                 event: error       {"message": "..."}
                 event: done        {"query_sql": ..., "chart_spec": ...}
   |
   `-- persist the assistant Message with content + query_sql + chart_spec
```

A few things in there are load-bearing and worth calling out.

**The model decides whether to query - there is no Python branching.**
There is no "does this user have a connection" gate choosing between canned
prompts. `AGENT_SYSTEM_PROMPT` tells the model: greetings, small talk and
questions about the product are answered directly with no tool call;
anything about the user's data must go through `run_query` first, never
from assumption and never from the sample rows in the schema context. Which
means "hi" gets a greeting, not an awkward *"I ran SELECT 1 and found
nothing"*.

**With no database bound to the chat, the model is given no tools at all.**
Not just told not to use them - `tools` is `None` on the request. It
physically cannot request a query when there is nothing to query, and the
prompt tells it to ask the user to connect a database instead.

**The model can never name a user, a connection, or an engine.**
`run_query`'s schema has exactly one parameter: `query`, a string.
`render_chart`'s has four: a chart type and three labels. `user_id`,
`connection_id`, `engine_name` and the credentials are captured by
`app/api/messages.py` from the authenticated request *before* the streaming
generator runs, and are the only values the executor ever uses. No prompt
injection - in a table name, in a column comment, in the user's own message
- has a parameter to reach for. (They are also captured as plain values for
a second, more boring reason: by the time a `StreamingResponse` generator
executes, FastAPI has closed the request-scoped session and the ORM objects
are detached.)

**`render_chart` cannot invent data.** It has no data parameters. The rows
it charts are the rows from the most recent `run_query` in that same turn,
held in a local variable inside `rag.py`. A model that tries to smuggle a
`"rows"` key into the arguments is simply ignored - there's a test for
exactly that.

**A failed query is a conversation, not a 500.** A read-only violation, a
missing column, a timeout: all come back to the model as a tool message
("The query did not run. Error: ..."), and it apologizes and explains in
plain language, or corrects itself and tries once more within the round
budget. The raw driver exception never reaches the user.

**Transparency is persisted.** `messages.query_sql` records the literal
query text that ran for that turn, and `messages.chart_spec` records the
chart plus its data. Reloading the chat shows you exactly what was executed
against your database to produce each answer - which matters much more here
than in a document-RAG product, because the assistant is writing queries
against live systems.

---

## Read-only enforcement, per engine

The assistant writes the queries. "The prompt says SELECT only" is a
policy, not a control - so no model-authored query reaches a driver without
passing a real guard first, and even a query that passes runs inside a
transaction that is always rolled back.

### The five layers (all SQL engines)

Implemented in `app/engine/db_adapters/readonly.py` (pure functions,
standard library only - which is why the test suite can verify them without
a single database server) and applied by every adapter.

1. **Comments stripped, then the text must start with `SELECT` or `WITH`.**
   `--`, `#` and `/* */` comments are removed first, so commenting out a
   statement separator doesn't help. Anything not starting with SELECT/WITH
   is refused before the keyword scan even runs.
2. **No blocklisted keyword anywhere, as a standalone token.**
   `INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, GRANT, REVOKE,
   EXEC, EXECUTE, CALL, MERGE, REPLACE, ATTACH, DETACH, PRAGMA`. This is a
   **word-boundary regex, not substring matching** - a column named
   `updated_at`, `created_at`, `dropoff_rate` or `insertion_point` is not
   falsely blocked (`\bUPDATE\b` cannot match inside `updated_at`).
   String literals and quoted identifiers are **masked** before this scan,
   so `SELECT * FROM notes WHERE body = 'please delete everything'` is
   fine, and so is a column that genuinely has to be quoted (`"update"`,
   `` `grant` ``, `[merge]`).
3. **Exactly one statement.** A `;` followed by anything other than
   whitespace is refused, so `SELECT 1; DROP TABLE users` can't slip a
   second statement past the prefix check. A semicolon inside a string
   literal isn't a separator.
4. **The transaction is always rolled back, never committed** - defense in
   depth on top of 1-3.
5. **A row cap and a statement timeout**, both engine-correct (below).

Rejections raise `NotReadOnlyError`; database failures raise
`QueryExecutionError`; connection failures raise `ConnectionFailedError`.
Nothing else escapes an adapter - `app/api/messages.py` catches those three
and hands the message back to the model. A raw psycopg2/pymysql/pymssql/
pymongo traceback never reaches the API layer, let alone the user.

> **One deliberate false positive: `REPLACE`.** It's blocklisted because
> `REPLACE INTO` is a write on MySQL and SQLite - which also refuses the
> perfectly innocent `REPLACE(col, 'a', 'b')` string function. Refusing a
> legitimate query is the right side to err on here; ask for
> `SUBSTRING`/`||` or a client-side transform instead.

### PostgreSQL (`psycopg2` via SQLAlchemy)

The strongest of the five, because Postgres has a real server-side switch.

- `SET TRANSACTION READ ONLY` is the first statement inside the
  transaction. The **server** refuses any write for the rest of it,
  independently of whether our parsing is perfect.
- `SET LOCAL statement_timeout = <QUERY_TIMEOUT_SECONDS * 1000>` - a true
  server-side cancel. `LOCAL` scopes it to this transaction so it can't
  leak.
- Row cap: `SELECT * FROM (<query>) AS _sub LIMIT n`. Postgres accepts a
  `WITH` CTE inside a derived table, so this works for every query shape.
- `application_name=private-data-assistant`, so a DBA looking at
  `pg_stat_activity` can see exactly what these connections are.
- `extra_params`: `{"ssl_mode": "require"}` maps to psycopg2's `sslmode`.

### MySQL / MariaDB (`PyMySQL` via SQLAlchemy)

- `SET SESSION TRANSACTION READ ONLY` is issued **before** the transaction
  opens - MySQL applies that statement to *subsequent* transactions, not
  one already in progress. (This is why it lives in a different hook from
  Postgres' in-transaction version; getting it wrong makes it silently do
  nothing.)
- Timeout is attempted twice, because the two forks spell it differently
  and neither accepts the other's syntax: `SET SESSION MAX_EXECUTION_TIME =
  <ms>` (MySQL 5.7.8+, SELECT-only, which is exactly our case) and
  `SET SESSION max_statement_time = <seconds>` (MariaDB). Both are
  best-effort; an old server that rejects both still gets the connect
  timeout, socket read/write timeouts, and the row cap.
- Row cap: `LIMIT`, as Postgres.
- `extra_params`: `{"ssl_mode": "require"}` enables TLS;
  `{"ssl_ca": "/path/ca.pem"}` enables TLS *and* verifies the CA.

### SQL Server / MSSQL (`pymssql` via SQLAlchemy)

**Driver choice is a documented tradeoff.** pyodbc is the more featureful
and more commonly recommended SQL Server driver, but it needs a system ODBC
driver manager plus Microsoft's `msodbcsql18`/FreeTDS package in the image
(an apt key, an EULA flag, ~200MB) before it can connect at all. pymssql
bundles FreeTDS in its wheel, so `pip install pymssql` is all this
container needs. The cost:

- no `ApplicationIntent=ReadOnly` (an ODBC/MS-driver feature), so unlike
  Postgres there is **no server-side read-only switch** - MSSQL read-only
  rests on the static guard plus the always-rolled-back transaction. Give
  it a `SELECT`-only login (see
  [docs/testing-with-a-sample-database.md](docs/testing-with-a-sample-database.md))
  if that matters to you;
- no Azure AD / Entra authentication, and stricter modern TLS policies may
  refuse the connection.

Swapping to pyodbc later means changing two methods in
`mssql_adapter.py` and nothing else - that's the point of the adapter
boundary.

**T-SQL has no `LIMIT`.** The row cap is `SELECT TOP (n)` *before* the
select list, so `wrap_with_row_limit()` emits
`SELECT TOP (n) * FROM (<query>) AS _sub` for this engine. Two further
T-SQL rules make some queries unwrappable, and the wrapper **declines** to
wrap them rather than generating SQL Server will reject:

- a derived table may not contain a `WITH` CTE at all;
- a derived table may not contain `ORDER BY` unless it also has its own
  `TOP`/`OFFSET`.

Those queries are run as written and truncated to `MAX_QUERY_ROWS` in
Python instead - the cap still holds, it's just client-side. (The
`OFFSET 0 ROWS FETCH NEXT n ROWS ONLY` form would handle the ORDER BY case,
but it *requires* an ORDER BY to exist - exactly the opposite constraint -
so Python truncation is the simpler correct answer for both.) Every adapter
also falls back this way if a wrapped query fails for any other reason.

Timeout: pymssql's `timeout` connect argument is a real per-query timeout,
plus `SET LOCK_TIMEOUT` so a query can't wait forever on someone else's
lock.

### SQLite (stdlib `sqlite3`, on an uploaded file)

The one engine with no server: you upload the file, and it's stored at
`storage/{user_id}/{connection_id}/database.sqlite`. That path is built
server-side from the authenticated user id and the row's own id - **never**
from the uploaded filename - so there is no path-traversal surface.

- The file is opened through a URI with **`mode=ro`**. SQLite itself
  refuses every write at the C level: an `ATTACH`, a `PRAGMA
  journal_mode=WAL`, a `CREATE TABLE` all fail with *"attempt to write a
  readonly database"* even if they somehow got past the guard. This is a
  hard guarantee that doesn't depend on our parsing at all, and there's a
  test that bypasses the guard entirely to prove it.
- Row cap: `LIMIT`, as Postgres.
- **Timeouts are the weak spot, and this deviates from the obvious
  approach.** SQLite has no statement timeout, and a thread-based guard
  would abandon a thread that keeps running. Instead the adapter installs a
  `set_progress_handler` callback that fires every 1000 virtual-machine
  instructions and returns non-zero once the deadline passes, which makes
  SQLite genuinely **abort** the statement. A pathological query that
  spends all its time inside one long-running C call can still overrun, but
  for ordinary scans this is a real interrupt rather than a best-effort
  one.
- Introspection uses `PRAGMA table_info` / `PRAGMA foreign_key_list` rather
  than SQLAlchemy's `Inspector`, because this adapter is deliberately
  SQLAlchemy-free. (`PRAGMA` is on the blocklist - that blocklist governs
  *model-written* queries, not this module's own fixed SQL.)

### MongoDB (`pymongo`)

There is no SQL to parse, so the guard is an **operation whitelist**
instead. `run_query` receives a JSON operation spec, and the system prompt
tells the model so, conditioned on the connection's engine:

```json
{"operation": "find", "collection": "orders", "filter": {"status": "shipped"},
 "projection": {"_id": 0}, "sort": {"total": -1}, "limit": 20}
{"operation": "aggregate", "collection": "orders", "pipeline": [...]}
{"operation": "count",     "collection": "orders", "filter": {...}}
{"operation": "distinct",  "collection": "orders", "field": "status"}
```

1. It must parse as JSON and name one of exactly those four operations.
   Anything else (`insertOne`, `updateMany`, `drop`, `mapReduce`, ...) is
   refused.
2. `$out`, `$merge`, `$function`, `$accumulator` and `$where` are
   blocklisted. `$where` is a *read* operator, but it executes arbitrary
   server-side JavaScript, which a model-authored query does not get to do.
   **The blocklist is applied recursively, not just to top-level pipeline
   stage keys** - `{"filter": {"$where": "..."}}` on a plain `find` is
   exactly as dangerous, and is refused too.
3. The row cap is folded in: `.limit(max_rows)` for `find`, and a trailing
   `{"$limit": max_rows}` stage appended to an aggregation unless its final
   stage already caps at or below that. (Only the *final* stage counts - an
   earlier `$limit` says nothing about output size once a `$unwind` or
   `$lookup` follows it.)
4. `maxTimeMS` bounds every call server-side.

Results are flattened into the same `(columns, rows)` shape the SQL
adapters return - columns are the union of the returned documents' keys in
first-seen order, and a nested object/array is JSON-encoded into one cell -
so the rest of the system never needs to know which engine produced the
rows.

Caveat worth knowing: the "columns" in a Mongo collection's schema chunk
are **inferred from a 20-document sample**, so a field that only appears in
older documents may be missing from it, and the assistant will say it can't
see that field. Re-index after a schema change, or ask about it explicitly.

---

## Credential handling

Unlike this app's own account passwords (hashed one-way with bcrypt), a
registered database's password has to be handed back to a driver verbatim
every time a query runs - so it must be **encrypted**, not hashed.

- `app/core/crypto.py` uses `cryptography.fernet.Fernet` (AES-128-CBC +
  HMAC-SHA256 authenticated encryption) under a single deployment-wide key
  from **`ENCRYPTION_KEY`**. Generate one with:
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- **`ENCRYPTION_KEY` is required before any connection can be
  registered.** `POST /api/connections` returns
  `503 {"error": "encryption_not_configured"}` rather than storing a
  plaintext password or crashing. A truthy-but-malformed key reports as
  *unconfigured* (the check actually constructs a `Fernet`), so you get the
  clear 503 rather than a 500 at save time.
- Ciphertext goes into `database_connections.encrypted_password`. The
  plaintext is only ever materialized in memory, on the line before a
  driver connection is opened
  (`app/api/connections.py:build_connection_info`).
- **No API response ever contains either form.** `ConnectionOut` has **no
  password field at all** - not a redacted one, not a null one, not a
  `has_password` boolean that might later tempt someone into putting the
  value next to it. There is a test that asserts the password string
  appears in no response body and that no such key exists in any of them.
- Rotating `ENCRYPTION_KEY` makes every stored password undecryptable.
  Those connections then report `status: "failed"` with an explanation on
  their next test/reindex and must be re-saved. There is deliberately no
  key-rotation tooling in this pass.

---

## The `engine/` isolation contract and the generator handshake

`app/engine/` is a self-contained package. The contract (spelled out in
full in `app/engine/__init__.py`):

- nothing in it imports from `app.api`, `app.models`, `app.core.security`
  or `app.core.crypto`;
- every function takes and returns plain values - ints, strings, dicts,
  dataclasses - never an ORM object or a FastAPI `Request`/`Response`;
- configuration comes from environment variables directly, not from
  `app.core.config.Settings`; per-request policy (row cap, timeout) is
  passed in as arguments;
- credentials arrive already decrypted, as a plain `ConnectionInfo` - the
  engine doesn't know they're stored encrypted.

`app/api/connections.py` and `app/api/messages.py` are the only modules
allowed to touch both sides.

### The problem, and the handshake that solves it

`rag.py` must decide *whether* to run a query and *what* query to run - but
executing one needs a database driver and decrypted credentials, which
would drag the whole crypto/DB stack into `app/engine/` and collapse the
contract.

So **`stream_agentic_reply()` is an `AsyncGenerator[dict, dict]`**, driven
with `asend()` rather than `async for` (which cannot send values back in).
When the model asks to run a query, the generator yields a `tool_call`
event and *suspends on that yield* until the caller resumes it with the
real result:

```python
agen = stream_agentic_reply(user_id=..., connection_id=..., engine_name=...,
                            chat_history=..., message=..., schema_context=...)
to_send = None
while True:
    try:
        event = await agen.asend(to_send)
    except StopAsyncIteration:
        break
    to_send = None

    if event["type"] == "tool_call":                    # {"name": "run_query", "query": ...}
        to_send = execute_read_only_query(              # <- the ONLY place a query runs
            engine_name, connection_info, event["query"],
            max_rows, timeout_seconds)                  # -> {"ok":..., "columns":..., "rows":...}
    else:
        yield sse(event)                                # token / chart / error / done
```

The event carries `user_id`, `connection_id` and `engine` **echoed from the
generator's own arguments** so a caller can assert they match what it
passed - they never come from the model, which has no parameter for them,
and `messages.py` uses its own captured values regardless.

The alternative considered was passing an executor *callback* into
`rag.py`. The generator was chosen because it keeps the data flow visible
in one readable loop in `messages.py`, makes the suspension point explicit,
and means `rag.py` holds no reference to anything callable that could
touch a database.

---

## Getting started

### 1. Clone and configure

```bash
cp .env.example .env
```

Fill in `.env`:

- **`AZURE_EM_*`** - Azure OpenAI embeddings. Always required: without
  them a schema can't be indexed and a question can't be embedded.
- **A chat provider** - either leave `LLM_PROVIDER` unset/`groq` (the
  default) and set `GROQ_API_KEY`, or set `LLM_PROVIDER=azure` and fill in
  `LLM_ENDPOINT*`.
- **`ENCRYPTION_KEY`** - required to register any connection:
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- **`JWT_SECRET_KEY`** - required for auth:
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
- SMTP values are optional (forgot-password returns 503 until they're set).

`GET /api/config/status` tells you what's missing without you having to
guess:

```json
{"connections_llm": true, "encryption": true, "smtp": false, "llm_provider": "groq"}
```

`.env` is gitignored - never commit it.

### 2. Bring the stack up

```bash
docker-compose up --build
```

`postgres` (this app's metadata), `qdrant`, `backend` on
http://localhost:8000, and `frontend` on http://localhost:4173.
**No target databases** - those are yours; see
[docs/testing-with-a-sample-database.md](docs/testing-with-a-sample-database.md)
to spin up a throwaway one.

For frontend development against a locally-running backend, skip the
`frontend` container and use the Vite dev server instead - it proxies
`/api` to `http://localhost:8000`, so no CORS or base-URL configuration is
needed:

```bash
cd frontend
npm install
npm run dev     # http://localhost:5173
```

### 3. Run the migrations

```bash
docker-compose exec backend alembic upgrade head
```

Creates `users`, `password_reset_tokens`, `database_connections`, `chats`,
`messages`. From the host instead (note `localhost`, not the compose
service name):

```bash
cd backend
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/private_data_assistant \
  alembic upgrade head
```

### 4. Use it - the whole loop in the UI

Open http://localhost:4173 (or http://localhost:5173 for the dev server).
The landing page shows this deployment's live config status, so you can see
before signing up whether AI credentials and `ENCRYPTION_KEY` are in place.

1. **Sign up.** *Sign up* -> name, email, password (8+ characters with an
   upper, a lower, a digit and a symbol - the same policy the backend
   enforces). You land straight in the app; the session survives a reload,
   and an expired access token is refreshed silently.
2. **Add a database.** *My databases* in the sidebar -> **Add database**.
   Pick an engine and the form changes shape with it:
   - **PostgreSQL / MySQL / SQL Server / MongoDB** - name, host, port
     (blank = that engine's standard port), database name, username,
     password, and one optional extra (`SSL mode` for the SQL engines,
     `Auth source` for MongoDB).
   - **SQLite** - just a name and the `.sqlite`/`.db` file to upload.

   Submitting runs the whole registration synchronously - connect, read the
   schema, embed it - so it takes a few seconds and then reports its final
   state. A green **ready** pill with a "schema indexed" timestamp means
   you can query it. A red **failed** pill shows the backend's own
   explanation ("password authentication failed", "no tables this account
   can see"), which is written to be acted on rather than decoded.

   Each row also has **Test** (is it reachable right now? - doesn't touch
   the stored status), **Re-index** (run this after you change your
   schema), and **Delete**.

   Note what you can never see here, or anywhere else in the UI: the
   password you typed. `ConnectionOut` has no field for it in any form.
3. **Point a chat at it.** **New chat**, then choose the database from the
   **Database** selector in the chat header. Non-`ready` connections appear
   greyed out with their status. Selecting one sends
   `PATCH /api/chats/{id}` with just `{"connection_id": N}`; *No database*
   sends `null`. The sidebar shows each chat's binding underneath its
   title, and the pencil next to the title renames the chat inline.

   You don't have to pick one before typing - a chat with no database still
   handles greetings and questions about the product. You just get a
   one-line hint above the composer until you do.
4. **Ask a question.** Type *"what's total revenue by country?"* and watch
   the turn happen live:
   - **"Querying your database..."** appears the moment the model decides
     to run a query - expand **Show the query** to read the exact SQL (or
     Mongo pipeline) before it returns.
   - That line then flips to **"Query returned 12 rows"**, or to the error
     verbatim if it failed - which is not the end of the turn: the model is
     handed the error and explains or retries in the tokens that follow.
   - The answer streams in token by token, rendered as Markdown (the model
     is told to write result rows into a table when there is more than a
     row or two).
   - If the question deserves a chart, one is drawn inline - a real
     Chart.js bar/line/pie with hover tooltips, plus a **Show the data**
     toggle underneath it that reveals the exact rows it was built from.
5. **Reload the page.** Reopening the chat replays the transcript
   faithfully - including the chart and the collapsible **Query used**
   under each answer that ran one, because both are persisted on the
   message.

Try *"delete every cancelled order"* too. It is refused in plain language:
the read-only guard is upstream of the database, so the query is never sent
at all.

The rest of this section does the same walkthrough with `curl`, for anyone
who would rather see the wire format.

### 5. Sign up

```bash
curl -X POST http://localhost:8000/api/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","full_name":"Your Name","password":"A-long-Password-123!"}'
# -> {"access_token":"...","refresh_token":"...","token_type":"bearer"}

TOKEN=<paste the access_token>
```

### 6. Register your first connection

A network database:

```bash
curl -X POST http://localhost:8000/api/connections \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "Production analytics",
    "engine": "postgres",
    "host": "db.internal.example.com",
    "port": 5432,
    "database_name": "analytics",
    "username": "assistant_readonly",
    "password": "the-database-password",
    "extra_params": {"ssl_mode": "require"}
  }'
```

```json
{
  "id": 1,
  "name": "Production analytics",
  "engine": "postgres",
  "host": "db.internal.example.com",
  "port": 5432,
  "database_name": "analytics",
  "username": "assistant_readonly",
  "extra_params": {"ssl_mode": "require"},
  "status": "ready",
  "error_message": null,
  "schema_indexed_at": "2026-09-09T10:14:52.481Z",
  "created_at": "2026-09-09T10:14:49.002Z"
}
```

Note there is no password field in that response, and never will be. If
`status` comes back `"failed"`, `error_message` says why in plain language
(bad credentials, unreachable host, no tables visible to that account).

A SQLite file instead:

```bash
curl -X POST http://localhost:8000/api/connections/sqlite \
  -H "Authorization: Bearer $TOKEN" \
  -F "name=My local data" \
  -F "file=@/path/to/your.sqlite"
```

Test it without re-indexing, or re-index after a schema change:

```bash
curl -X POST http://localhost:8000/api/connections/1/test    -H "Authorization: Bearer $TOKEN"
curl -X POST http://localhost:8000/api/connections/1/reindex -H "Authorization: Bearer $TOKEN"
```

### 7. Create a chat and point it at that connection

```bash
curl -X POST http://localhost:8000/api/chats \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"connection_id": 1}'
# -> {"id": 1, "title": "New chat", "connection_id": 1, ...}
```

Or create it first and bind later - this is exactly what the chat header's
**Database** selector sends:

```bash
curl -X PATCH http://localhost:8000/api/chats/1 \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"connection_id": 1}'
# {"connection_id": null} unbinds it again
```

### 8. Ask a question - the answer streams back as SSE

```bash
curl -N -X POST http://localhost:8000/api/chats/1/messages \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"content":"what is our total revenue by country?"}'
```

```
event: query
data: {"query": "SELECT c.country, SUM(o.total) AS revenue FROM orders o JOIN customers c ON c.id = o.customer_id GROUP BY c.country ORDER BY revenue DESC"}

event: query_result
data: {"ok": true, "row_count": 3, "error": null}

event: token
data: {"content": "Revenue"}

event: token
data: {"content": " by country:"}

...

event: chart
data: {"chart_type": "bar", "title": "Revenue by country", "x_field": "country", "y_field": "revenue", "columns": ["country", "revenue"], "rows": [["US", "1049.99"], ["UK", "205.50"], ["DE", "130.25"]]}

event: done
data: {"query_sql": "SELECT c.country, SUM(o.total) ...", "chart_spec": {...}}
```

And two things to try that prove the guard and the routing:

```bash
# a greeting - no query at all, no `query` event
curl -N -X POST http://localhost:8000/api/chats/1/messages \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"content":"hi, what can you do?"}'

# an instruction to write - refused, in plain language
curl -N -X POST http://localhost:8000/api/chats/1/messages \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"content":"delete every cancelled order"}'
```

### 9. Other useful URLs

- The app: http://localhost:4173 (docker-compose) or http://localhost:5173
  (`npm run dev`)
- Health check: http://localhost:8000/health
- Config status: http://localhost:8000/api/config/status
- Interactive API docs: http://localhost:8000/docs
- Qdrant dashboard: http://localhost:6333/dashboard

### Running the backend without Docker

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate      # Windows  (source .venv/bin/activate elsewhere)
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

---

## API reference

Every endpoint below requires `Authorization: Bearer <access_token>` except
`/health`, `/api/config/status` and `/api/auth/*`. Everywhere, a resource
that exists but belongs to another user returns **404, not 403** - so
ownership can't be distinguished from non-existence.

### Auth (`/api/auth/*`)

| Endpoint | Notes |
|---|---|
| `POST /api/auth/signup` | `{email, full_name, password}` -> `{access_token, refresh_token}`. Password policy: 8+ chars, upper, lower, digit, special. |
| `POST /api/auth/login` | verifies the password, returns tokens |
| `POST /api/auth/refresh` | exchanges a refresh token for a new access token (a refresh token can't be replayed as an access token - they carry a `type` claim) |
| `GET /api/auth/me` | the current user |
| `POST /api/auth/forgot-password` | issues a 1-hour token and emails a link - **503** (`smtp_not_configured`) if SMTP isn't set up |
| `POST /api/auth/reset-password` | `{token, new_password}` - 400 if invalid/expired/used |

### Connections (`/api/connections/*`)

| Endpoint | Notes |
|---|---|
| `POST /api/connections` | JSON body for `postgres`/`mysql`/`mssql`/`mongodb`: `{name, engine, host, port, database_name, username, password, extra_params}`. Encrypts the password, creates the row `status=pending`, then synchronously tests -> introspects -> indexes, returning the row as `ready` or `failed`. **503** `encryption_not_configured` / `ai_not_configured`. **Never 500s** - a failure is a `failed` row. Rejects `engine=sqlite` and points at the route below. |
| `POST /api/connections/sqlite` | multipart (`file`, `name`) - stores the upload at `storage/{user_id}/{connection_id}/database.sqlite` and runs the same pipeline. (Two routes rather than one because a single FastAPI route can't cleanly describe both a JSON body and a multipart upload - see `connections.py`.) |
| `GET /api/connections` | the caller's connections, newest first |
| `GET /api/connections/{id}` | one connection |
| `POST /api/connections/{id}/test` | re-runs only the connectivity check -> `{ok, error}`. Deliberately does **not** change `status`: a momentarily-unreachable database shouldn't demote a `ready` connection. |
| `POST /api/connections/{id}/reindex` | deletes the old Qdrant points and re-introspects + re-embeds, so a dropped table disappears from the index |
| `DELETE /api/connections/{id}` | deletes the row, its Qdrant points, and (for SQLite) the stored file. Chats that used it keep their transcript but are unbound (`ON DELETE SET NULL`). |

No response from any of these contains the password in any form.

### Chats (`/api/chats/*`)

| Endpoint | Notes |
|---|---|
| `POST /api/chats` | `{title?, connection_id?}`. Both optional. Creating with no title and no connection **reuses** an existing empty untitled chat (returns 200 instead of 201) so a "+ New chat" button can't stack duplicates. 404 if `connection_id` isn't yours. |
| `GET /api/chats` | the caller's chats, newest first |
| `GET /api/chats/{id}` | the chat plus its messages, each with `query_sql` and `chart_spec` |
| `PATCH /api/chats/{id}` | `{title?, connection_id?}`. Sets/changes the bound database (404 unless it's yours), or clears it with an explicit `{"connection_id": null}`. Omitting the key leaves the binding alone - presence, not None-ness, is what marks a field as being changed. |
| `DELETE /api/chats/{id}` | deletes the chat and its messages |

### Messages (`/api/chats/{chat_id}/messages`)

| Endpoint | Notes |
|---|---|
| `POST /api/chats/{chat_id}/messages` | `{content}` - that's the entire body; there is nothing in it that can influence which database is queried. Persists the user message, retrieves schema context from Qdrant, and streams the reply as SSE. Persists the assistant message with `content` + `query_sql` + `chart_spec` when the turn finishes. **404** if the chat (or its connection) isn't yours; **503** if AI isn't configured; **400** for empty content. |

SSE event types: `token` (`{content}`), `query` (`{query}` - the query
about to run), `query_result` (`{ok, row_count, error}`), `chart`
(`{chart_type, title, x_field, y_field, columns, rows}`), `error`
(`{message}`), `done` (`{query_sql, chart_spec}`).

Read the stream with `fetch` + a `ReadableStream` reader, not the browser
`EventSource` API - `EventSource` can't attach an `Authorization` header.

### Config (`/api/config/status`)

```json
{"connections_llm": true, "encryption": true, "smtp": false, "llm_provider": "groq"}
```

- `connections_llm` - embeddings (`AZURE_EM_*`, always Azure) **AND**
  whichever chat provider `LLM_PROVIDER` selects. Never both providers'
  credentials at once. `AZURE_EM_DIMENSIONS` is not required (it has a
  code default of 1536).
- `encryption` - `ENCRYPTION_KEY` is set *and* is a usable Fernet key.
- `smtp` - forgot-password email delivery.

---

## Environment variables

All live in `.env` (gitignored), documented with blank placeholders in
`.env.example`. Most of the backend reads them via `app/core/config.py` (a
pydantic `Settings` model); the exception is `app/engine/`, which reads
`os.environ` directly so that package has zero dependency on the rest of
the app. Either way the values come from the same file - docker-compose's
`env_file: .env` exports every one of them into the container.

| Variable | Purpose | Required |
|---|---|---|
| `DATABASE_URL` | Postgres connection string for **this app's own** metadata database | Yes |
| `QDRANT_URL` | Qdrant base URL (local container or Qdrant Cloud) | Yes |
| `QDRANT_API_KEY` | Qdrant Cloud API key (blank for the local container) | Optional |
| `QDRANT_COLLECTION` | Collection holding schema vectors | Optional (default `private_data_assistant_schema`) |
| `FRONTEND_URL` | Used to build password-reset links | Yes |
| `VITE_API_BASE_URL` | Read by docker-compose as a **build arg** for the frontend image (Vite inlines `VITE_*` at build time) | Yes, for the compose frontend build |
| `STORAGE_DIR` | Where uploaded SQLite files go, as `{STORAGE_DIR}/{user_id}/{connection_id}/database.sqlite` | Optional (default `storage`) |
| `ENCRYPTION_KEY` | Fernet key encrypting registered databases' passwords. Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | **Yes** - no connection can be registered without it |
| `MAX_QUERY_ROWS` | Row cap on every query the assistant runs | Optional (default `200`) |
| `QUERY_TIMEOUT_SECONDS` | Statement timeout on every query | Optional (default `15`) |
| `AZURE_EM_ENDPOINT` | Azure OpenAI embeddings endpoint | Yes, for anything to work |
| `AZURE_EM_API_KEY` | Embeddings API key | Yes |
| `AZURE_EM_API_VERSION` | Embeddings API version | Yes |
| `AZURE_EM_MODEL` | Embeddings deployment name | Yes |
| `AZURE_EM_DIMENSIONS` | Vector size, sizes the Qdrant collection | Optional (default `1536` in code) |
| `LLM_PROVIDER` | Which provider serves **chat** - `groq` (default) or `azure`. Embeddings are always Azure. | Optional |
| `GROQ_API_KEY` | Groq key - needed when `LLM_PROVIDER=groq` | Optional |
| `GROQ_LLM_MODEL` | Groq chat model | Optional (default `openai/gpt-oss-120b`) |
| `LLM_ENDPOINT` / `LLM_ENDPOINT_APIKEY` / `LLM_MODEL_NAME` | Azure OpenAI chat - needed when `LLM_PROVIDER=azure` | Optional |
| `LLM_ENDPOINT_API_VERSION` | Azure chat API version | Optional (falls back to `AZURE_EM_API_VERSION`, then a code default) |
| `JWT_SECRET_KEY` | Signs JWTs. Generate: `python -c "import secrets; print(secrets.token_hex(32))"` | Yes |
| `JWT_ALGORITHM` / `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` / `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | Token settings | Optional (`HS256` / `30` / `7`) |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_FROM_EMAIL` | Forgot-password email | Optional - 503 until all are set |

> **Groq model deprecations.** Groq retires model ids periodically. If a
> chat call fails with `model_not_found` or `model_decommissioned`, update
> `GROQ_LLM_MODEL` in `.env` - nothing in the code needs to change.

---

## Running tests

```bash
docker-compose exec backend pytest tests/ -v
```

Or from the host, pointing the Qdrant tests at the published port (the
hostname `qdrant` only resolves inside the docker network):

```bash
cd backend
pip install -r requirements.txt
QDRANT_TEST_URL=http://localhost:6333 pytest tests/ -v
```

| File | Covers |
|---|---|
| `test_schema_qdrant_isolation.py` | **The most important file here.** `schema_rag`'s search isolation against a **real** (disposable, uniquely-named) Qdrant collection: a `user_id` mismatch returns nothing *even when `connection_id` matches*; a `connection_id` mismatch returns nothing *even when `user_id` matches*; two users reusing the same numeric `connection_id` never leak into each other (in both directions, including when queried with the *other* user's exact vector, proving the filter and not similarity decides); identically-named tables across users don't overwrite each other; one user's two connections never bleed; deletion is scoped by both ids; re-indexing overwrites rather than duplicating. Plus the chunk renderer (columns, PK/FK markers, sample-row table, truncation). |
| `test_db_adapters_readonly.py` | The read-only guard as pure functions - `SELECT`/`WITH` pass; every blocklisted keyword is rejected as a standalone token *and* not falsely matched inside `updated_at`/`dropoff_rate`; keywords inside string literals and quoted identifiers are fine; multi-statement strings are rejected including behind a comment; MSSQL `TOP`-wrapping vs Postgres/MySQL/SQLite `LIMIT`-wrapping, and MSSQL declining to wrap a CTE or an ORDER BY; the whole MongoDB spec guard (four allowed operations, five blocked operators including `$where` nested in a filter, limit clamping and `$limit` injection). Plus the SQLite adapter end-to-end against a real temp database: rows, row cap, guard rejection, `mode=ro` refusing a write even when the guard is bypassed, PK/FK/sample introspection, and a missing file reported rather than raised. |
| `test_rag.py` | The agentic loop and the generator handshake with a mocked provider - a greeting never runs a query; the first call carries the system prompt, schema context and both tools; no connection means no tools are offered at all; a tool round-trip passes exactly the `user_id`/`connection_id`/`engine_name` the caller supplied (proving the model can't override them); rows/failures/truncation/empty results are fed back correctly; `render_chart` uses the held rows and **ignores** data the model tries to smuggle into its arguments; the final round is issued with no tools so the loop terminates; dialect hints; a provider failure becomes an `error` event rather than an exception. |
| `test_messages_api.py` | The endpoint that executes queries - the isolation invariant (extra fields in the request body can't redirect the query; the executor gets the endpoint's own engine and freshly-decrypted credentials, not the ids echoed in the event), SSE plumbing, chart + `query_sql` persistence and reload, the no-connection path, auto-titling, and the 503/404/400 gates. |
| `test_connections_api.py` | Ownership, and the credential rules - the password never appears in any response and no such field exists in the schema; it is stored encrypted, round-trips, and is what reaches the adapter; the `pending -> indexing -> ready/failed` machine for connect failures, introspection failures, indexing failures and an empty database; validation; the SQLite upload path and its server-derived storage path; test/reindex/delete behavior. Adapters and indexing are mocked, so no real database is needed. |
| `test_chats_api.py` | CRUD, ownership 404s, and the connection binding - set, change, clear with an explicit null, leave alone when omitted, and 404 when binding to another user's connection. |
| `test_auth_api.py` | `/api/auth/*` - signup/duplicate/password policy, login, refresh-token type confusion, and the `jwt_not_configured`/`smtp_not_configured` 503 gates. |
| `test_config_status.py` | The provider-aware `connections_llm` group (never both providers at once, `AZURE_EM_DIMENSIONS` not required), the `encryption` group including a malformed key reporting `false`, `smtp`, and `llm_provider` defaulting. |

**Test database choice:** the integration tests run against an in-memory
SQLite database via a `get_db` dependency override, not the docker-compose
Postgres - nothing they exercise depends on Postgres-specific behavior, and
a fresh zero-setup schema per test makes "this row must not be visible to
that user" trivial to reason about. Full rationale in
`backend/tests/conftest.py`'s module docstring. This is not a substitute
for `alembic upgrade head` against real Postgres.

`test_schema_qdrant_isolation.py` is the deliberate exception: Qdrant's
payload filtering is the single property this whole product's multi-tenancy
rests on, so it is tested against a real Qdrant instance. And
`test_db_adapters_readonly.py` needs no server at all, by design -
`readonly.py` imports nothing but the standard library precisely so the
security-critical logic can be unit-tested anywhere.

---

## Roadmap / known tradeoffs

Things that are deliberately scoped out of this pass, with what a
production version would do instead.

- **Tokens live in `localStorage`.** Simple, and it survives a reload, but
  it is readable by any script that gets injected into the page. A
  production version would use an httpOnly, SameSite refresh cookie with
  the access token held in memory only.
- **The connection list doesn't poll.** Registration is synchronous (see
  the next bullet), so a row's `status` is final by the time the UI sees
  it and there is nothing to poll for today. The moment indexing moves to
  a background worker, `ConnectionsModal` needs a poll or a subscription
  for rows sitting in `indexing`.
- **Raw query results are only available where a chart was drawn.** The
  SSE `query_result` event carries `{ok, row_count, error}` - not the rows
  - so the UI cannot offer a result grid for every query. Rows reach the
  client only inside a `chart` event (and the `chart_spec` persisted with
  it), which is why "show the data" hangs off a chart. For everything
  else, the model writes the rows into its answer as a Markdown table. A
  future version could add the rows to `query_result` (bounded by
  `MAX_QUERY_ROWS`) and render a grid for every query.
- **Schema indexing is synchronous**, inside the registration request -
  the simplest thing that works, and a database with a few dozen tables
  indexes in seconds. A large schema (hundreds of tables) will make that
  request slow. Production would push it onto a background worker (Celery,
  RQ, arq) behind a queue, return immediately with `status=indexing`, and
  let the client poll `GET /api/connections` until it flips. Everything
  else about the pipeline already works the way it would in that design -
  the status machine exists precisely so this can be swapped without
  changing the API contract.
- **No query result caching.** Every question re-runs its query. Fine for
  interactive use; a busy deployment would want a short-lived cache keyed
  on (connection, query text).
- **`MAX_TABLES_INTROSPECTED = 200`** per connection, and `top_k=8` tables
  of schema context per question. Both are fixed constants rather than
  per-connection settings, and a very wide schema will exceed the first.
- **No key rotation tooling.** Rotating `ENCRYPTION_KEY` invalidates every
  stored credential; each connection must be re-saved with its password. A
  real deployment would keep a key id alongside each ciphertext and
  re-encrypt in the background.
- **MSSQL has no server-side read-only mode** with pymssql (see the MSSQL
  section). Give it a `SELECT`-only login.
- **SQLite timeouts are interrupt-based, not preemptive** - a query stuck
  inside a single long C call can overrun.
- **`REPLACE` is over-blocked** - the string function is refused along with
  `REPLACE INTO`.
- **Deployment hardening.** This targets local docker-compose. A real
  deployment also needs CORS tightened from `allow_origins=["*"]`, HTTPS
  termination, a secrets manager instead of `.env`, per-user rate limits on
  query execution, and an audit log of executed queries beyond
  `messages.query_sql`.
