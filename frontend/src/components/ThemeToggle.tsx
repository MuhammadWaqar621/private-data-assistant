import { Monitor, Moon, Sun } from "lucide-react";

import { useTheme } from "../lib/theme";
import type { ThemeMode } from "../lib/theme";

const OPTIONS: { mode: ThemeMode; label: string; icon: typeof Sun }[] = [
  { mode: "light", label: "Light", icon: Sun },
  { mode: "dark", label: "Dark", icon: Moon },
  { mode: "system", label: "System", icon: Monitor },
];

/**
 * A 3-way light/dark/system segmented control - persisted (see lib/theme.tsx)
 * and applied globally via Tailwind's `dark` class strategy, so it's the
 * single control for the whole app (before and after login alike).
 */
export default function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const { mode, setMode } = useTheme();

  return (
    <div
      className="inline-flex items-center gap-0.5 rounded-lg border border-slate-200 bg-white p-0.5 dark:border-slate-700 dark:bg-slate-800"
      role="radiogroup"
      aria-label="Theme"
    >
      {OPTIONS.map(({ mode: optionMode, label, icon: Icon }) => (
        <button
          key={optionMode}
          type="button"
          role="radio"
          aria-checked={mode === optionMode}
          title={label}
          onClick={() => setMode(optionMode)}
          className={`flex items-center justify-center gap-1 rounded-md px-2 py-1.5 text-xs font-medium transition ${
            mode === optionMode
              ? "bg-brand-600 text-white"
              : "text-slate-500 hover:bg-slate-100 dark:text-slate-400 dark:hover:bg-slate-700"
          }`}
        >
          <Icon size={14} />
          {!compact && <span>{label}</span>}
        </button>
      ))}
    </div>
  );
}
