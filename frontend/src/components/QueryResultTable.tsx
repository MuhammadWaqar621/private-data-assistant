/**
 * The `columns` + `rows` of an executed query, as a plain scrollable
 * table.
 *
 * Where these rows come from is worth being precise about, because it
 * bounds where this component can be used: the SSE `query_result` event
 * carries only `{ok, row_count, error}` - the actual rows never cross the
 * wire on their own. The ONLY payload that carries real rows is a `chart`
 * event (and the identical `chart_spec` persisted on the message), which
 * the backend emits only when the model calls its render_chart tool. So
 * this table is offered as a "show the underlying numbers" disclosure
 * beneath a chart, and as ChartAdapter's fallback when a chart's
 * x_field/y_field don't resolve against its own columns - not as a
 * general per-query result grid, which the API simply does not expose.
 *
 * For a question that returns tabular data without a chart, the model has
 * already been instructed (AGENT_SYSTEM_PROMPT) to write the rows into its
 * answer as a markdown table, which MarkdownMessage renders.
 */

type Props = {
  columns: string[];
  rows: (string | number | null)[][];
  /** Optional caption shown above the table (e.g. a fallback explanation). */
  caption?: string;
};

export default function QueryResultTable({ columns, rows, caption }: Props) {
  if (columns.length === 0 || rows.length === 0) {
    return (
      <p className="text-xs text-slate-400 dark:text-slate-500">
        The query returned no rows.
      </p>
    );
  }

  return (
    <div>
      {caption && (
        <p className="mb-1.5 text-xs text-slate-500 dark:text-slate-400">{caption}</p>
      )}
      <div className="max-h-64 overflow-auto rounded-lg border border-slate-200 dark:border-slate-700">
        <table className="w-full border-collapse text-left text-xs">
          <thead className="sticky top-0 bg-slate-50 dark:bg-slate-800">
            <tr>
              {columns.map((column) => (
                <th
                  key={column}
                  className="whitespace-nowrap border-b border-slate-200 px-2.5 py-1.5 font-semibold text-slate-600 dark:border-slate-700 dark:text-slate-300"
                >
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex} className="odd:bg-white even:bg-slate-50/60 dark:odd:bg-slate-900 dark:even:bg-slate-800/40">
                {columns.map((column, columnIndex) => (
                  <td
                    key={column}
                    className="whitespace-nowrap px-2.5 py-1.5 text-slate-700 dark:text-slate-300"
                  >
                    {row[columnIndex] === null || row[columnIndex] === undefined ? (
                      <span className="text-slate-300 dark:text-slate-600">null</span>
                    ) : (
                      String(row[columnIndex])
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
