/**
 * Chart-spec validation, shared by the SSE parser (lib/chatStream.ts, for
 * a live `chart` event) and the transcript (components/ChartAdapter.tsx,
 * for a `chart_spec` replayed from history). Both sources are typed
 * `Dict[str, Any]` server-side, so neither is trusted structurally here -
 * anything that doesn't validate is dropped rather than crashing a render.
 */

import type { ChartSpec, ChartType } from "./types";

const CHART_TYPES: ChartType[] = ["bar", "line", "pie"];

function isRow(value: unknown): value is (string | number | null)[] {
  return (
    Array.isArray(value) &&
    value.every((cell) => cell === null || typeof cell === "string" || typeof cell === "number")
  );
}

/** Narrow an untrusted value to a ChartSpec, or null if it isn't one. */
export function asChartSpec(value: unknown): ChartSpec | null {
  if (!value || typeof value !== "object") return null;
  const spec = value as Partial<ChartSpec>;

  if (typeof spec.chart_type !== "string") return null;
  if (!CHART_TYPES.includes(spec.chart_type as ChartType)) return null;
  if (!Array.isArray(spec.columns) || !spec.columns.every((c) => typeof c === "string")) {
    return null;
  }
  if (!Array.isArray(spec.rows) || !spec.rows.every(isRow)) return null;

  return {
    chart_type: spec.chart_type as ChartType,
    title: typeof spec.title === "string" ? spec.title : "",
    x_field: typeof spec.x_field === "string" ? spec.x_field : "",
    y_field: typeof spec.y_field === "string" ? spec.y_field : "",
    columns: spec.columns,
    rows: spec.rows,
  };
}

/**
 * Coerce one cell into a number for a chart's value axis. The adapters
 * hand numeric columns back as-is where the driver produces a number, but
 * DECIMAL/NUMERIC columns commonly arrive as strings (Postgres `numeric`
 * via psycopg2 is a Decimal, serialized as "1049.99" by the SSE layer's
 * `json.dumps(..., default=str)`), so a plain `Number()` on the raw cell
 * is the right coercion and NaN means "genuinely not plottable".
 */
export function toChartNumber(cell: string | number | null): number {
  if (typeof cell === "number") return cell;
  if (cell === null) return NaN;
  const parsed = Number(cell);
  return Number.isFinite(parsed) ? parsed : NaN;
}
