import { Link } from "react-router-dom";
import { BarChart3, Database, Lock, ShieldCheck } from "lucide-react";

import ThemeToggle from "../components/ThemeToggle";
import { useConfigStatus } from "../lib/useConfigStatus";

function Logo() {
  return (
    <div className="flex items-center gap-2">
      <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-600 text-white">
        <Database size={16} strokeWidth={2.5} />
      </div>
      <span className="text-lg font-bold tracking-tight text-slate-900 dark:text-white">
        Private Data Assistant
      </span>
    </div>
  );
}

function StatusPill({ label, ok }: { label: string; ok: boolean }) {
  return (
    <div
      className={`flex items-center justify-between rounded-lg border px-3 py-1.5 text-xs ${
        ok
          ? "border-green-200 bg-green-50 text-green-800 dark:border-green-800 dark:bg-green-950 dark:text-green-300"
          : "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-300"
      }`}
    >
      <span className="font-medium">{label}</span>
      <span>{ok ? "configured" : "missing"}</span>
    </div>
  );
}

const features = [
  {
    icon: ShieldCheck,
    title: "Read-only, always",
    description:
      "Every query the assistant writes is checked before it runs and executed inside a transaction that is always rolled back. It can read your data. It cannot change it.",
  },
  {
    icon: Database,
    title: "Your database stays yours",
    description:
      "PostgreSQL, MySQL/MariaDB, SQL Server, SQLite or MongoDB. Nothing is copied out - only your schema is indexed, so the assistant knows which tables a question is about.",
  },
  {
    icon: BarChart3,
    title: "Answers, not SQL homework",
    description:
      "Ask in plain English. The reply streams in as it's written, shows you the exact query that ran, and draws a chart when the numbers deserve one.",
  },
];

const steps = [
  {
    title: "Register a database",
    body: "Host, port, credentials - or just drag in a .sqlite file. The schema is read once and indexed; your rows stay where they are.",
  },
  {
    title: "Pick it for a chat",
    body: "Each chat points at one database. Switch it any time from the selector in the chat header.",
  },
  {
    title: "Ask anything",
    body: '"How many orders shipped late last month?" "Chart revenue by country." The query runs against your live data and the answer comes back with it.',
  },
];

export default function HomePage() {
  const { status, error } = useConfigStatus();

  return (
    <div className="flex min-h-screen flex-col bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <header className="border-b border-slate-200 bg-white/80 backdrop-blur dark:border-slate-800 dark:bg-slate-950/80">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <Logo />
          <nav className="flex items-center gap-3">
            <ThemeToggle compact />
            <Link
              to="/login"
              className="rounded-lg px-4 py-2 text-sm font-medium text-slate-600 transition hover:text-slate-900 dark:text-slate-300 dark:hover:text-white"
            >
              Log in
            </Link>
            <Link
              to="/signup"
              className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white shadow-card transition hover:bg-brand-700"
            >
              Sign up
            </Link>
          </nav>
        </div>
      </header>

      <main className="flex-1">
        <section className="mx-auto max-w-3xl px-6 pb-16 pt-20 text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-brand-200 bg-brand-50 px-3 py-1 text-xs font-medium text-brand-700 dark:border-brand-800 dark:bg-brand-950 dark:text-brand-300">
            <Lock size={12} /> Read-only &amp; private by default
          </span>
          <h1 className="mt-6 text-4xl font-extrabold tracking-tight text-slate-900 dark:text-white sm:text-5xl">
            Your database. Your questions.
            <br />
            <span className="text-brand-600 dark:text-brand-400">No SQL required.</span>
          </h1>
          <p className="mx-auto mt-5 max-w-xl text-lg text-slate-600 dark:text-slate-400">
            Connect a database you already have and just ask it things. The
            assistant reads your schema, writes the query, runs it read-only
            against your live data, and answers from the rows that come back.
          </p>
          <div className="mt-8 flex items-center justify-center gap-3">
            <Link
              to="/signup"
              className="rounded-lg bg-brand-600 px-6 py-3 text-sm font-semibold text-white shadow-card transition hover:bg-brand-700"
            >
              Get started free
            </Link>
            <Link
              to="/login"
              className="rounded-lg border border-slate-300 bg-white px-6 py-3 text-sm font-semibold text-slate-700 shadow-card transition hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800"
            >
              Log in
            </Link>
          </div>
        </section>

        <section className="mx-auto max-w-5xl px-6 pb-20">
          <div className="grid gap-6 sm:grid-cols-3">
            {features.map(({ icon: Icon, title, description }) => (
              <div
                key={title}
                className="rounded-xl border border-slate-200 bg-white p-6 shadow-card dark:border-slate-800 dark:bg-slate-900"
              >
                <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-lg bg-brand-50 text-brand-600 dark:bg-brand-950 dark:text-brand-400">
                  <Icon size={20} strokeWidth={2} />
                </div>
                <h3 className="font-semibold text-slate-900 dark:text-white">{title}</h3>
                <p className="mt-1.5 text-sm leading-relaxed text-slate-600 dark:text-slate-400">
                  {description}
                </p>
              </div>
            ))}
          </div>
        </section>

        <section className="mx-auto max-w-5xl px-6 pb-20">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400 dark:text-slate-500">
            How it works
          </h2>
          <ol className="mt-4 grid gap-4 sm:grid-cols-3">
            {steps.map((step, index) => (
              <li
                key={step.title}
                className="rounded-xl border border-slate-200 bg-white p-5 shadow-card dark:border-slate-800 dark:bg-slate-900"
              >
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-brand-600 text-xs font-bold text-white">
                  {index + 1}
                </span>
                <h3 className="mt-3 font-semibold text-slate-900 dark:text-white">
                  {step.title}
                </h3>
                <p className="mt-1.5 text-sm leading-relaxed text-slate-600 dark:text-slate-400">
                  {step.body}
                </p>
              </li>
            ))}
          </ol>
        </section>

        <section className="mx-auto max-w-5xl px-6 pb-20">
          <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-card dark:border-slate-800 dark:bg-slate-900">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400 dark:text-slate-500">
              Live demo status
            </h2>
            <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
              This deployment's backend configuration, checked in real time.
              Sign-up and login work regardless of what's shown below;
              registering a database and asking questions need the first two.
            </p>

            {error && (
              <p className="mt-4 text-sm text-red-600 dark:text-red-400">
                Could not reach the backend ({error}). Is it running?
              </p>
            )}

            {!error && !status && (
              <p className="mt-4 text-sm text-slate-500 dark:text-slate-400">
                Checking configuration...
              </p>
            )}

            {status && (
              <div className="mt-4 grid gap-2 sm:grid-cols-2">
                <StatusPill
                  label={`AI (embeddings + ${status.llm_provider === "azure" ? "Azure" : "Groq"} chat)`}
                  ok={status.connections_llm}
                />
                <StatusPill label="Credential encryption" ok={status.encryption} />
                <StatusPill label="SMTP (email)" ok={status.smtp} />
              </div>
            )}
          </div>
        </section>
      </main>

      <footer className="border-t border-slate-200 bg-white px-6 py-6 text-center text-xs text-slate-400 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-500">
        Private Data Assistant - ask your own databases questions in plain
        language, read-only and private.
      </footer>
    </div>
  );
}
