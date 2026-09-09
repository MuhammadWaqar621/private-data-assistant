import { useMemo, useState } from "react";
import {
  ArcElement,
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  Filler,
  Legend,
  LineElement,
  LinearScale,
  PointElement,
  Title,
  Tooltip,
} from "chart.js";
import { Bar, Line, Pie } from "react-chartjs-2";
import { Table } from "lucide-react";

import QueryResultTable from "./QueryResultTable";
import { toChartNumber } from "../lib/chart";
import type { ChartSpec } from "../lib/types";

ChartJS.register(
  CategoryScale,
  LinearScale,
  BarElement,
  LineElement,
  PointElement,
  ArcElement,
  Title,
  Tooltip,
  Legend,
  Filler,
);

// A palette matching the app's brand color, cycled across pie slices -
// distinct enough to stay readable, not a jarring rainbow. Same set the
// sibling project's ChartBlock uses, so the two products' charts look
// like they came from the same place.
const PALETTE = [
  "#7161ec", // brand-500
  "#22c55e", // emerald-500
  "#f59e0b", // amber-500
  "#ef4444", // red-500
  "#0ea5e9", // sky-500
  "#ec4899", // pink-500
  "#84cc16", // lime-500
  "#a855f7", // purple-500
];

const PRIMARY = PALETTE[0];

/**
 * Renders one `chart` SSE event (or a `chart_spec` replayed from history)
 * as a real Chart.js chart.
 *
 * The input shape here is NOT the sibling project's `{labels, datasets}`:
 * the backend sends the raw query result (`columns` + `rows`) plus the two
 * column NAMES to plot (`x_field`, `y_field`), and building Chart.js's
 * shape from that is this component's job. Doing the mapping client-side
 * is what makes the "show the underlying rows" disclosure below free - the
 * real data is already here, no second request.
 *
 * Everything is checked defensively: the backend only ever emits bar/line/
 * pie, but a field name that doesn't match any column (a model slip, or a
 * spec persisted before a schema change) falls back to rendering the raw
 * table rather than throwing inside a chart render.
 */
export default function ChartAdapter({ spec }: { spec: ChartSpec }) {
  const [showTable, setShowTable] = useState(false);

  const mapped = useMemo(() => {
    const xIndex = spec.columns.indexOf(spec.x_field);
    const yIndex = spec.columns.indexOf(spec.y_field);
    if (xIndex === -1 || yIndex === -1) return null;

    const labels = spec.rows.map((row) => String(row[xIndex] ?? ""));
    const values = spec.rows.map((row) => toChartNumber(row[yIndex]));
    // A chart whose value axis is entirely non-numeric isn't a chart -
    // it's a table the model mislabeled.
    if (values.length === 0 || values.every((value) => Number.isNaN(value))) return null;

    return { labels, values: values.map((value) => (Number.isNaN(value) ? 0 : value)) };
  }, [spec]);

  if (!mapped) {
    return (
      <div className="mb-2 rounded-lg border border-slate-200 bg-white p-3 last:mb-0 dark:border-slate-700 dark:bg-slate-950">
        <QueryResultTable
          columns={spec.columns}
          rows={spec.rows}
          caption={
            spec.title
              ? `${spec.title} - showing the raw rows (this chart's columns couldn't be plotted).`
              : "Showing the raw rows (this chart's columns couldn't be plotted)."
          }
        />
      </div>
    );
  }

  const { labels, values } = mapped;
  const isPie = spec.chart_type === "pie";

  const legendTitleOptions = {
    legend: {
      display: isPie,
      position: "bottom" as const,
      labels: { boxWidth: 12, font: { size: 11 } },
    },
    title: {
      display: Boolean(spec.title),
      text: spec.title,
      font: { size: 13, weight: "bold" as const },
    },
  };

  // Two separate option objects rather than one conditional: a pie has no
  // cartesian scales, and Chart.js's per-type option types reflect that.
  const cartesianOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: legendTitleOptions,
    scales: {
      x: { grid: { display: false } },
      y: { beginAtZero: true },
    },
  };

  const circularOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: legendTitleOptions,
  };

  const barData = {
    labels,
    datasets: [
      {
        label: spec.y_field,
        data: values,
        backgroundColor: PRIMARY,
        borderColor: PRIMARY,
        borderWidth: 1,
      },
    ],
  };

  const lineData = {
    labels,
    datasets: [
      {
        label: spec.y_field,
        data: values,
        backgroundColor: `${PRIMARY}33`,
        borderColor: PRIMARY,
        borderWidth: 2,
        pointBackgroundColor: PRIMARY,
        fill: true,
        tension: 0.3,
      },
    ],
  };

  const pieData = {
    labels,
    datasets: [
      {
        label: spec.y_field,
        data: values,
        backgroundColor: labels.map((_, index) => PALETTE[index % PALETTE.length]),
        borderWidth: 1,
      },
    ],
  };

  return (
    <div className="mb-2 rounded-lg border border-slate-200 bg-white p-3 last:mb-0 dark:border-slate-700 dark:bg-slate-950">
      <div style={{ height: 280 }}>
        {spec.chart_type === "bar" && <Bar data={barData} options={cartesianOptions} />}
        {spec.chart_type === "line" && <Line data={lineData} options={cartesianOptions} />}
        {spec.chart_type === "pie" && <Pie data={pieData} options={circularOptions} />}
      </div>

      <button
        type="button"
        onClick={() => setShowTable((prev) => !prev)}
        className="mt-2 flex items-center gap-1.5 text-xs font-medium text-slate-500 transition hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
      >
        <Table size={13} />
        {showTable
          ? "Hide the data"
          : `Show the data (${spec.rows.length} ${spec.rows.length === 1 ? "row" : "rows"})`}
      </button>

      {showTable && (
        <div className="mt-2">
          <QueryResultTable columns={spec.columns} rows={spec.rows} />
        </div>
      )}
    </div>
  );
}
