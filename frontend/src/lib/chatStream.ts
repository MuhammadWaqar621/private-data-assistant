/**
 * Streams POST /api/chats/{chatId}/messages as Server-Sent Events using a
 * manual fetch + ReadableStream reader, NOT the browser EventSource API -
 * EventSource only supports GET and can't attach a custom Authorization
 * header, but this endpoint is bearer-token protected like every other
 * /api/* call in this app. This yields real incremental tokens as they
 * arrive from the backend, not a fake progress bar.
 *
 * The event set (see backend/app/api/messages.py's event_stream) is:
 *
 *   token        {"content": "..."}                incremental answer text
 *   query        {"query": "..."}                  a query is ABOUT to run
 *   query_result {"ok", "row_count", "error"}      that query just finished
 *   chart        a full ChartSpec                  render a chart right here
 *   done         {"query_sql", "chart_spec"}       turn over; both persisted
 *   error        {"message": "..."}                hard failure this turn
 *
 * `query_result` with ok=false is NOT the end of the turn - the model is
 * handed the error and usually explains or retries in its next tokens, so
 * the caller should show it as a live status line and keep reading.
 *
 * A single retry-after-refresh (mirroring api.ts's request()) is not
 * implemented here for simplicity - if the access token has expired,
 * onError() fires with a 401 and the caller can prompt the user to
 * re-login rather than silently retrying mid-stream.
 */

import { apiUrl } from "./config";
import { getAccessToken } from "./auth";
import { asChartSpec } from "./chart";
import type { ChartSpec } from "./types";

export type QueryResult = {
  ok: boolean;
  row_count: number;
  error: string | null;
};

export type DonePayload = {
  query_sql: string | null;
  chart_spec: ChartSpec | null;
};

export type StreamCallbacks = {
  onToken: (text: string) => void;
  /** The model just decided to run this query; it has NOT executed yet. */
  onQuery: (query: string) => void;
  /** That query finished - `ok: false` carries a human-readable `error`. */
  onQueryResult: (result: QueryResult) => void;
  onChart: (spec: ChartSpec) => void;
  onError: (message: string) => void;
  onDone: (payload: DonePayload) => void;
};

export async function streamChatMessage(
  chatId: number,
  content: string,
  callbacks: StreamCallbacks,
): Promise<void> {
  const token = getAccessToken();

  let res: Response;
  try {
    res = await fetch(apiUrl(`/api/chats/${chatId}/messages`), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ content }),
    });
  } catch {
    callbacks.onError("Could not reach the server.");
    return;
  }

  if (!res.ok || !res.body) {
    callbacks.onError(await describeError(res));
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let separatorIndex = buffer.indexOf("\n\n");
    while (separatorIndex !== -1) {
      const rawEvent = buffer.slice(0, separatorIndex);
      buffer = buffer.slice(separatorIndex + 2);
      handleEvent(rawEvent, callbacks);
      separatorIndex = buffer.indexOf("\n\n");
    }
  }
}

async function describeError(res: Response): Promise<string> {
  try {
    const body = await res.json();
    const detail = body?.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && typeof detail.message === "string") {
      return detail.message;
    }
  } catch {
    // fall through to the generic message below
  }
  return `Request failed with status ${res.status}`;
}

function handleEvent(rawEvent: string, callbacks: StreamCallbacks): void {
  let eventName = "message";
  let data = "";
  for (const line of rawEvent.split("\n")) {
    if (line.startsWith("event:")) eventName = line.slice("event:".length).trim();
    // Trimming is safe: every payload is a JSON object, so any meaningful
    // leading/trailing whitespace in a token lives inside its quotes.
    else if (line.startsWith("data:")) data += line.slice("data:".length).trim();
  }
  if (!data) return;

  let parsed: Record<string, unknown>;
  try {
    const value: unknown = JSON.parse(data);
    if (!value || typeof value !== "object") return;
    parsed = value as Record<string, unknown>;
  } catch {
    return;
  }

  switch (eventName) {
    case "token": {
      if (typeof parsed.content === "string" && parsed.content) {
        callbacks.onToken(parsed.content);
      }
      break;
    }
    case "query": {
      callbacks.onQuery(typeof parsed.query === "string" ? parsed.query : "");
      break;
    }
    case "query_result": {
      callbacks.onQueryResult({
        ok: Boolean(parsed.ok),
        row_count: typeof parsed.row_count === "number" ? parsed.row_count : 0,
        error: typeof parsed.error === "string" ? parsed.error : null,
      });
      break;
    }
    case "chart": {
      const spec = asChartSpec(parsed);
      if (spec) callbacks.onChart(spec);
      break;
    }
    case "done": {
      callbacks.onDone({
        query_sql: typeof parsed.query_sql === "string" ? parsed.query_sql : null,
        chart_spec: asChartSpec(parsed.chart_spec),
      });
      break;
    }
    case "error": {
      callbacks.onError(
        typeof parsed.message === "string"
          ? parsed.message
          : "The assistant encountered an error.",
      );
      break;
    }
    default:
      break;
  }
}
