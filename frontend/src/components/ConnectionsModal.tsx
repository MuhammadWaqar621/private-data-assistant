import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Database,
  Loader2,
  Plug,
  Plus,
  RefreshCw,
  Trash2,
  Upload,
  X,
} from "lucide-react";

import { ApiError, api, isEncryptionNotConfigured } from "../lib/api";
import { CONNECTION_STATUS_STYLES, ENGINES, engineMeta, formatTimestamp } from "../lib/engines";
import type { ConnectionOut, DatabaseEngine, TestResult } from "../lib/types";

type Props = {
  open: boolean;
  onClose: () => void;
  connections: ConnectionOut[];
  loading: boolean;
  /** Re-fetch the list in the parent after any mutation here. */
  onChanged: () => Promise<void>;
  onAuthFailure: () => void;
  /** GET /api/config/status -> `encryption`. False disables "Add database". */
  encryptionConfigured: boolean;
  /** GET /api/config/status -> `connections_llm`. False disables it too. */
  aiConfigured: boolean;
};

const inputClass =
  "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm transition focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 disabled:cursor-not-allowed disabled:bg-slate-50 dark:border-slate-700 dark:bg-slate-800 dark:text-white dark:disabled:bg-slate-900";
const labelClass = "mb-1 block text-xs font-medium text-slate-600 dark:text-slate-400";

const EMPTY_FORM = {
  name: "",
  host: "",
  port: "",
  databaseName: "",
  username: "",
  password: "",
  extraValue: "",
};

/**
 * The "My databases" library: every external database this account has
 * registered, plus the form to add another.
 *
 * Registration is SYNCHRONOUS server-side (test -> introspect schema ->
 * embed into Qdrant), so submitting can take a few seconds and the
 * returned row already carries its final `status` - there is nothing to
 * poll. A `failed` row's `error_message` is written to be read by a human
 * ("password authentication failed", "no tables this account can see"), so
 * it is shown verbatim rather than replaced with something generic.
 *
 * Nothing here ever displays a password: `ConnectionOut` has no field for
 * one, in any form. The password box below is write-only - it goes into
 * the create request and is never read back.
 */
export default function ConnectionsModal({
  open,
  onClose,
  connections,
  loading,
  onChanged,
  onAuthFailure,
  encryptionConfigured,
  aiConfigured,
}: Props) {
  const [showForm, setShowForm] = useState(false);
  const [engine, setEngine] = useState<DatabaseEngine>("postgres");
  const [form, setForm] = useState(EMPTY_FORM);
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [lastCreated, setLastCreated] = useState<ConnectionOut | null>(null);

  const [busyId, setBusyId] = useState<number | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<number, TestResult>>({});
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Reset the transient bits every time the modal is reopened, so a stale
  // "failed" banner from a previous session isn't the first thing seen.
  useEffect(() => {
    if (!open) return;
    setFormError(null);
    setRowError(null);
    setLastCreated(null);
  }, [open]);

  if (!open) return null;

  const meta = engineMeta(engine);
  const canAdd = encryptionConfigured && aiConfigured;

  function resetForm() {
    setForm(EMPTY_FORM);
    setFile(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  function handleApiError(err: unknown, fallback: string): string {
    if (err instanceof ApiError && err.status === 401) {
      onAuthFailure();
      return "";
    }
    if (isEncryptionNotConfigured(err)) {
      // The backend's own message names the exact command to generate a
      // Fernet key, so pass it through rather than paraphrasing it.
      return err instanceof Error
        ? err.message
        : "ENCRYPTION_KEY is not set, so a database password cannot be stored.";
    }
    return err instanceof Error ? err.message : fallback;
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (submitting || !canAdd) return;

    setFormError(null);
    setLastCreated(null);

    const name = form.name.trim();
    if (!name) {
      setFormError("Give this connection a name.");
      return;
    }

    if (meta.isFile && !file) {
      setFormError("Choose a .sqlite or .db file to upload.");
      return;
    }
    if (!meta.isFile) {
      if (!form.host.trim()) {
        setFormError(`A host is required for a ${meta.label} connection.`);
        return;
      }
      if (!form.databaseName.trim()) {
        setFormError("A database name is required.");
        return;
      }
    }

    setSubmitting(true);
    try {
      let created: ConnectionOut;

      if (meta.isFile && file) {
        // POST /api/connections/sqlite - multipart, `file` + `name`.
        const formData = new FormData();
        formData.append("file", file);
        formData.append("name", name);
        created = await api.uploadFile<ConnectionOut>("/api/connections/sqlite", formData);
      } else {
        const extraParams: Record<string, string> = {};
        const extraValue = form.extraValue.trim();
        if (meta.extraParam && extraValue) {
          extraParams[meta.extraParam.key] = extraValue;
        }

        created = await api.post<ConnectionOut>(
          "/api/connections",
          {
            name,
            engine,
            host: form.host.trim(),
            // Omitted entirely when blank, so the backend applies the
            // engine's standard default port rather than receiving a 0.
            ...(form.port.trim() ? { port: Number(form.port.trim()) } : {}),
            database_name: form.databaseName.trim(),
            ...(form.username.trim() ? { username: form.username.trim() } : {}),
            ...(form.password ? { password: form.password } : {}),
            extra_params: extraParams,
          },
          true,
        );
      }

      setLastCreated(created);
      if (created.status !== "failed") {
        resetForm();
        setShowForm(false);
      }
      await onChanged();
    } catch (err) {
      const message = handleApiError(err, "Could not register that database.");
      if (message) setFormError(message);
    } finally {
      setSubmitting(false);
    }
  }

  async function testConnection(id: number) {
    setBusyId(id);
    setRowError(null);
    try {
      const result = await api.post<TestResult>(`/api/connections/${id}/test`, undefined, true);
      setTestResults((prev) => ({ ...prev, [id]: result }));
    } catch (err) {
      const message = handleApiError(err, "Could not test that connection.");
      if (message) setRowError(message);
    } finally {
      setBusyId(null);
    }
  }

  async function reindexConnection(id: number) {
    setBusyId(id);
    setRowError(null);
    try {
      await api.post<ConnectionOut>(`/api/connections/${id}/reindex`, undefined, true);
      await onChanged();
    } catch (err) {
      const message = handleApiError(err, "Could not re-index that database.");
      if (message) setRowError(message);
    } finally {
      setBusyId(null);
    }
  }

  async function deleteConnection(id: number) {
    setBusyId(id);
    setRowError(null);
    try {
      await api.del(`/api/connections/${id}`, true);
      setTestResults((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      await onChanged();
    } catch (err) {
      const message = handleApiError(err, "Could not delete that connection.");
      if (message) setRowError(message);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4"
      onClick={onClose}
    >
      <div
        className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-xl bg-white shadow-lg dark:bg-slate-900"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4 dark:border-slate-800">
          <div className="flex items-center gap-2">
            <Database size={18} className="text-brand-600 dark:text-brand-400" />
            <h2 className="font-semibold dark:text-white">My databases</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-full p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800 dark:hover:text-slate-300"
          >
            <X size={18} />
          </button>
        </div>

        <p className="border-b border-slate-200 px-5 py-3 text-xs text-slate-500 dark:border-slate-800 dark:text-slate-400">
          Databases you register here stay where they are - only their{" "}
          <strong className="font-semibold">schema</strong> (table and column names, plus
          a few sample rows) is indexed so the assistant can work out which tables a
          question is about. Every query it runs is read-only. Re-index after you change
          your schema.
        </p>

        {!encryptionConfigured && (
          <p className="flex items-start gap-2 border-b border-amber-200 bg-amber-50 px-5 py-3 text-xs text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-300">
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            <span>
              Configuration missing - <code>ENCRYPTION_KEY</code> is not set, so a
              database password cannot be stored securely and adding a database is
              disabled. Generate a Fernet key and set it in <code>.env</code>, then
              restart the backend.
            </span>
          </p>
        )}

        {encryptionConfigured && !aiConfigured && (
          <p className="flex items-start gap-2 border-b border-amber-200 bg-amber-50 px-5 py-3 text-xs text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-300">
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            <span>
              Configuration missing - AI credentials aren't set, so a schema can't be
              indexed and adding a database is disabled.
            </span>
          </p>
        )}

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {loading && <p className="text-sm text-slate-400 dark:text-slate-500">Loading...</p>}

          {!loading && connections.length === 0 && (
            <p className="text-sm text-slate-400 dark:text-slate-500">
              No databases yet - add one below to start asking questions about your data.
            </p>
          )}

          {!loading && connections.length > 0 && (
            <ul className="flex flex-col gap-2">
              {connections.map((connection) => {
                const rowMeta = engineMeta(connection.engine);
                const RowIcon = rowMeta.icon;
                const test = testResults[connection.id];
                const busy = busyId === connection.id;
                return (
                  <li
                    key={connection.id}
                    className="rounded-lg border border-slate-200 px-3 py-2.5 dark:border-slate-700"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="flex min-w-0 items-start gap-2">
                        <span
                          title={rowMeta.label}
                          className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md ${rowMeta.badgeClass}`}
                        >
                          <RowIcon size={13} />
                        </span>
                        <div className="min-w-0">
                          <p className="truncate text-sm font-medium text-slate-800 dark:text-slate-200">
                            {connection.name}
                          </p>
                          <p className="truncate text-xs text-slate-500 dark:text-slate-400">
                            {rowMeta.label} &middot;{" "}
                            {connection.engine === "sqlite"
                              ? connection.database_name
                              : `${connection.host ?? "?"}:${connection.port ?? "?"}/${connection.database_name}`}
                          </p>
                          <p className="mt-0.5 text-xs text-slate-400 dark:text-slate-500">
                            Schema indexed: {formatTimestamp(connection.schema_indexed_at)}
                          </p>
                        </div>
                      </div>

                      <div className="flex shrink-0 items-center gap-1">
                        <span
                          className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                            CONNECTION_STATUS_STYLES[connection.status] ??
                            CONNECTION_STATUS_STYLES.pending
                          }`}
                        >
                          {connection.status}
                        </span>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => testConnection(connection.id)}
                          title="Test that this database is reachable right now"
                          className="rounded-full p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600 disabled:opacity-40 dark:hover:bg-slate-800 dark:hover:text-slate-300"
                        >
                          {busy ? (
                            <Loader2 size={14} className="animate-spin" />
                          ) : (
                            <Plug size={14} />
                          )}
                        </button>
                        <button
                          type="button"
                          disabled={busy || !aiConfigured}
                          onClick={() => reindexConnection(connection.id)}
                          title="Re-index the schema (run this after changing your database's schema)"
                          className="rounded-full p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-600 disabled:opacity-40 dark:hover:bg-slate-800 dark:hover:text-slate-300"
                        >
                          <RefreshCw size={14} />
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => deleteConnection(connection.id)}
                          title="Delete this connection"
                          className="rounded-full p-1.5 text-slate-400 transition hover:bg-red-50 hover:text-red-600 disabled:opacity-40 dark:hover:bg-red-950 dark:hover:text-red-400"
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </div>

                    {connection.error_message && (
                      <p className="mt-2 rounded-md bg-red-50 px-2.5 py-1.5 text-xs text-red-700 dark:bg-red-950 dark:text-red-300">
                        {connection.error_message}
                      </p>
                    )}

                    {test && (
                      <p
                        className={`mt-2 flex items-start gap-1.5 rounded-md px-2.5 py-1.5 text-xs ${
                          test.ok
                            ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300"
                            : "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300"
                        }`}
                      >
                        {test.ok ? (
                          <CheckCircle2 size={13} className="mt-0.5 shrink-0" />
                        ) : (
                          <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                        )}
                        <span>
                          {test.ok
                            ? "Reachable - credentials work and the database answered."
                            : test.error ?? "Could not reach this database."}
                        </span>
                      </p>
                    )}
                  </li>
                );
              })}
            </ul>
          )}

          {rowError && (
            <p className="mt-3 text-xs text-red-600 dark:text-red-400">{rowError}</p>
          )}
        </div>

        <div className="border-t border-slate-200 px-5 py-4 dark:border-slate-800">
          {!showForm && (
            <button
              type="button"
              disabled={!canAdd}
              onClick={() => setShowForm(true)}
              title={
                canAdd
                  ? "Register another database"
                  : "Disabled - see the configuration notice above"
              }
              className="flex w-fit items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 shadow-sm transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300 dark:hover:bg-slate-800"
            >
              <Plus size={13} />
              Add database
            </button>
          )}

          {showForm && (
            <form onSubmit={handleSubmit} className="flex flex-col gap-3">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold dark:text-white">Add a database</h3>
                <button
                  type="button"
                  onClick={() => {
                    setShowForm(false);
                    setFormError(null);
                  }}
                  className="text-xs text-slate-400 hover:text-slate-600 dark:hover:text-slate-300"
                >
                  Cancel
                </button>
              </div>

              <div>
                <label htmlFor="engine" className={labelClass}>
                  Engine
                </label>
                <select
                  id="engine"
                  value={engine}
                  disabled={submitting}
                  onChange={(e) => {
                    setEngine(e.target.value as DatabaseEngine);
                    setFormError(null);
                  }}
                  className={inputClass}
                >
                  {ENGINES.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label htmlFor="conn-name" className={labelClass}>
                  Name
                </label>
                <input
                  id="conn-name"
                  type="text"
                  required
                  disabled={submitting}
                  placeholder="Production analytics"
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  className={inputClass}
                />
              </div>

              {meta.isFile ? (
                <div>
                  <label className={labelClass}>Database file</label>
                  <button
                    type="button"
                    disabled={submitting}
                    onClick={() => fileInputRef.current?.click()}
                    className="flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-slate-300 px-3 py-6 text-xs text-slate-500 transition hover:border-brand-400 hover:text-brand-600 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-700 dark:text-slate-400 dark:hover:border-brand-500 dark:hover:text-brand-400"
                  >
                    <Upload size={14} />
                    {file ? file.name : "Choose a .sqlite or .db file"}
                  </button>
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept=".sqlite,.sqlite3,.db"
                    className="hidden"
                    onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                  />
                  <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
                    The file is uploaded once and stored privately against your account.
                    There is no host, port, or password for a SQLite database.
                  </p>
                </div>
              ) : (
                <>
                  <div className="grid grid-cols-3 gap-2">
                    <div className="col-span-2">
                      <label htmlFor="conn-host" className={labelClass}>
                        Host
                      </label>
                      <input
                        id="conn-host"
                        type="text"
                        required
                        disabled={submitting}
                        placeholder="db.internal.example.com"
                        value={form.host}
                        onChange={(e) => setForm({ ...form, host: e.target.value })}
                        className={inputClass}
                      />
                    </div>
                    <div>
                      <label htmlFor="conn-port" className={labelClass}>
                        Port
                      </label>
                      <input
                        id="conn-port"
                        type="number"
                        disabled={submitting}
                        placeholder={
                          meta.defaultPort ? `${meta.defaultPort} (default)` : "default"
                        }
                        value={form.port}
                        onChange={(e) => setForm({ ...form, port: e.target.value })}
                        className={inputClass}
                      />
                    </div>
                  </div>

                  <div>
                    <label htmlFor="conn-database" className={labelClass}>
                      Database name
                    </label>
                    <input
                      id="conn-database"
                      type="text"
                      required
                      disabled={submitting}
                      placeholder="analytics"
                      value={form.databaseName}
                      onChange={(e) => setForm({ ...form, databaseName: e.target.value })}
                      className={inputClass}
                    />
                  </div>

                  <div className="grid grid-cols-2 gap-2">
                    <div>
                      <label htmlFor="conn-username" className={labelClass}>
                        Username
                      </label>
                      <input
                        id="conn-username"
                        type="text"
                        autoComplete="off"
                        disabled={submitting}
                        placeholder="assistant_readonly"
                        value={form.username}
                        onChange={(e) => setForm({ ...form, username: e.target.value })}
                        className={inputClass}
                      />
                    </div>
                    <div>
                      <label htmlFor="conn-password" className={labelClass}>
                        Password
                      </label>
                      <input
                        id="conn-password"
                        type="password"
                        autoComplete="new-password"
                        disabled={submitting}
                        value={form.password}
                        onChange={(e) => setForm({ ...form, password: e.target.value })}
                        className={inputClass}
                      />
                    </div>
                  </div>

                  {meta.extraParam && (
                    <div>
                      <label htmlFor="conn-extra" className={labelClass}>
                        {meta.extraParam.label}
                      </label>
                      <input
                        id="conn-extra"
                        type="text"
                        disabled={submitting}
                        placeholder={meta.extraParam.placeholder}
                        value={form.extraValue}
                        onChange={(e) => setForm({ ...form, extraValue: e.target.value })}
                        className={inputClass}
                      />
                      <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
                        {meta.extraParam.hint}
                      </p>
                    </div>
                  )}

                  <p className="text-xs text-slate-400 dark:text-slate-500">
                    Use a read-only database user wherever you can. The password is
                    encrypted before it is stored and is never sent back to this page.
                  </p>
                </>
              )}

              {formError && (
                <p className="text-xs text-red-600 dark:text-red-400">{formError}</p>
              )}

              {lastCreated && (
                <div
                  className={`rounded-lg px-3 py-2 text-xs ${
                    lastCreated.status === "ready"
                      ? "bg-emerald-50 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300"
                      : lastCreated.status === "failed"
                        ? "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300"
                        : "bg-amber-50 text-amber-800 dark:bg-amber-950 dark:text-amber-300"
                  }`}
                >
                  <p className="font-medium">
                    {lastCreated.name} - {lastCreated.status}
                  </p>
                  {lastCreated.status === "ready" && (
                    <p className="mt-0.5">
                      Schema indexed {formatTimestamp(lastCreated.schema_indexed_at)}. Pick it
                      in a chat's database selector and start asking questions.
                    </p>
                  )}
                  {lastCreated.error_message && (
                    <p className="mt-0.5">{lastCreated.error_message}</p>
                  )}
                </div>
              )}

              <button
                type="submit"
                disabled={submitting || !canAdd}
                className="flex w-full items-center justify-center gap-2 rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white shadow-card transition hover:bg-brand-700 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {submitting && <Loader2 size={15} className="animate-spin" />}
                {submitting ? "Connecting and indexing the schema..." : "Add database"}
              </button>
              {submitting && (
                <p className="text-center text-xs text-slate-400 dark:text-slate-500">
                  This runs the connection test, reads the schema and embeds it - a few
                  seconds for a normal database.
                </p>
              )}
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
