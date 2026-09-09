/**
 * Per-engine presentation metadata: label, icon, badge color, the default
 * port the backend fills in when the form leaves it blank, and which
 * `extra_params` key that engine actually understands.
 *
 * There is no per-database-vendor logo set in lucide-react (and shipping
 * real vendor marks would mean bundling trademarked art), so each engine
 * gets a generic lucide glyph plus a distinct colored badge - enough to
 * tell five rows apart at a glance, which is all this needs to do.
 */

import { Database, HardDrive, Layers, Leaf, Server } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import type { DatabaseEngine } from "./types";

export type ExtraParamField = {
  /** The key written into `extra_params` on the create request. */
  key: string;
  label: string;
  placeholder: string;
  hint: string;
};

export type EngineMeta = {
  value: DatabaseEngine;
  label: string;
  icon: LucideIcon;
  /** Filled in server-side when the port field is left blank. */
  defaultPort: number | null;
  badgeClass: string;
  /** SQLite is registered by uploading a file, not by host/port. */
  isFile: boolean;
  extraParam: ExtraParamField | null;
};

const SSL_MODE: ExtraParamField = {
  key: "ssl_mode",
  label: "SSL mode",
  placeholder: "require",
  hint: "Optional. Passed through to the driver as extra_params.ssl_mode.",
};

export const ENGINES: EngineMeta[] = [
  {
    value: "postgres",
    label: "PostgreSQL",
    icon: Database,
    defaultPort: 5432,
    badgeClass: "bg-sky-50 text-sky-700 dark:bg-sky-950 dark:text-sky-300",
    isFile: false,
    extraParam: SSL_MODE,
  },
  {
    value: "mysql",
    label: "MySQL / MariaDB",
    icon: Server,
    defaultPort: 3306,
    badgeClass: "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
    isFile: false,
    extraParam: SSL_MODE,
  },
  {
    value: "mssql",
    label: "SQL Server",
    icon: Layers,
    defaultPort: 1433,
    badgeClass: "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300",
    isFile: false,
    extraParam: SSL_MODE,
  },
  {
    value: "sqlite",
    label: "SQLite (upload a file)",
    icon: HardDrive,
    defaultPort: null,
    badgeClass: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
    isFile: true,
    extraParam: null,
  },
  {
    value: "mongodb",
    label: "MongoDB",
    icon: Leaf,
    defaultPort: 27017,
    badgeClass: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
    isFile: false,
    extraParam: {
      key: "auth_source",
      label: "Auth source",
      placeholder: "admin",
      hint: "Optional. The database to authenticate against (extra_params.auth_source).",
    },
  },
];

const BY_VALUE: Record<DatabaseEngine, EngineMeta> = ENGINES.reduce(
  (acc, meta) => {
    acc[meta.value] = meta;
    return acc;
  },
  {} as Record<DatabaseEngine, EngineMeta>,
);

export function engineMeta(engine: DatabaseEngine): EngineMeta {
  return BY_VALUE[engine] ?? BY_VALUE.postgres;
}

/** Color coding for a connection's indexing status pill. */
export const CONNECTION_STATUS_STYLES: Record<string, string> = {
  pending: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-400",
  indexing: "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
  ready: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
  failed: "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300",
};

/** "9 Sep 2026, 10:14" - short, locale-aware, and tolerant of a null. */
export function formatTimestamp(value: string | null): string {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
