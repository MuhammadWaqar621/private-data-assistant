/**
 * Mirrors of the backend's response models. Every shape here was read off
 * backend/app/api/*.py, not guessed - notably ConnectionOut, which has no
 * password field of any kind and never will (see the docstring on
 * app/api/connections.py's ConnectionOut).
 */

export type ConfigStatus = {
  // Everything needed to register a connection AND chat about it:
  // Azure OpenAI embeddings (always) plus whichever chat provider
  // `llm_provider` names below. Gates the composer.
  connections_llm: boolean;
  // ENCRYPTION_KEY is set and is a usable Fernet key. Without it,
  // POST /api/connections returns 503 - so this gates "Add database".
  encryption: boolean;
  smtp: boolean;
  llm_provider: "groq" | "azure";
};

export type CurrentUser = {
  id: number;
  email: string;
  full_name: string | null;
};

// --- Connections -------------------------------------------------------------

export type DatabaseEngine = "postgres" | "mysql" | "mssql" | "sqlite" | "mongodb";

export type ConnectionStatus = "pending" | "indexing" | "ready" | "failed";

export type ConnectionOut = {
  id: number;
  name: string;
  engine: DatabaseEngine;
  host: string | null;
  port: number | null;
  database_name: string;
  username: string | null;
  extra_params: Record<string, unknown>;
  status: ConnectionStatus;
  error_message: string | null;
  schema_indexed_at: string | null;
  created_at: string;
};

/** POST /api/connections/{id}/test - live reachability only; deliberately
 * does not change the connection's stored `status`. */
export type TestResult = {
  ok: boolean;
  error: string | null;
};

// --- Charts ------------------------------------------------------------------

export type ChartType = "bar" | "line" | "pie";

/**
 * The payload of an SSE `chart` event, and (identically) the `chart_spec`
 * persisted on an assistant message. `rows` is an array of row-arrays
 * aligned positionally to `columns`; `x_field`/`y_field` name two of those
 * columns. See ChartAdapter.tsx for the mapping into Chart.js's
 * {labels, datasets} shape.
 */
export type ChartSpec = {
  chart_type: ChartType;
  title: string;
  x_field: string;
  y_field: string;
  columns: string[];
  rows: (string | number | null)[][];
};

// --- Chats -------------------------------------------------------------------

export type Chat = {
  id: number;
  title: string;
  connection_id: number | null;
  created_at: string;
};

export type MessageRole = "user" | "assistant";

export type ChatMessage = {
  id: number;
  role: MessageRole;
  content: string;
  // The query that actually ran for this turn (null when none was needed)
  // and the chart rendered from its rows - both persisted server-side so a
  // reload shows exactly what the user saw live.
  query_sql: string | null;
  chart_spec: ChartSpec | null;
  created_at: string;
};

export type ChatDetail = Chat & {
  messages: ChatMessage[];
};
