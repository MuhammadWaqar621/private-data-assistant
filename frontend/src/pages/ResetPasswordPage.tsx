import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";

import AuthLayout from "../components/AuthLayout";
import { ApiError, api } from "../lib/api";
import { PASSWORD_POLICY_HINT, passwordPolicyError } from "../lib/passwordPolicy";

const inputClass =
  "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-700 dark:bg-slate-800 dark:text-white";
const labelClass = "mb-1 block text-sm font-medium text-slate-700 dark:text-slate-300";

export default function ResetPasswordPage() {
  const [searchParams] = useSearchParams();
  const token = searchParams.get("token") ?? "";

  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setMessage(null);

    if (newPassword !== confirmPassword) {
      setError("Passwords don't match.");
      return;
    }
    const policyError = passwordPolicyError(newPassword);
    if (policyError) {
      setError(policyError);
      return;
    }

    setSubmitting(true);
    try {
      const res = await api.post<{ message: string }>("/api/auth/reset-password", {
        token,
        new_password: newPassword,
      });
      setMessage(res.message);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Something went wrong. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  if (!token) {
    return (
      <AuthLayout title="Reset your password">
        <p className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:bg-amber-950 dark:text-amber-300">
          This link is missing a reset token. Request a new one from the{" "}
          <Link to="/forgot-password" className="font-medium underline">
            forgot password
          </Link>{" "}
          page.
        </p>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout title="Reset your password">
      {message ? (
        <div className="flex flex-col gap-3">
          <p className="rounded-lg bg-green-50 px-3 py-2 text-sm text-green-800 dark:bg-green-950 dark:text-green-300">
            {message}
          </p>
          <Link
            to="/login"
            className="w-full rounded-lg bg-brand-600 px-4 py-2 text-center text-sm font-medium text-white shadow-card transition hover:bg-brand-700"
          >
            Go to log in
          </Link>
        </div>
      ) : (
        <form onSubmit={handleSubmit} className="flex flex-col gap-3">
          <div>
            <label htmlFor="newPassword" className={labelClass}>
              New password
            </label>
            <input
              id="newPassword"
              type="password"
              required
              autoComplete="new-password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              className={inputClass}
            />
            <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">
              {PASSWORD_POLICY_HINT}
            </p>
          </div>

          <div>
            <label htmlFor="confirmPassword" className={labelClass}>
              Confirm new password
            </label>
            <input
              id="confirmPassword"
              type="password"
              required
              autoComplete="new-password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              className={inputClass}
            />
          </div>

          {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

          <button
            type="submit"
            disabled={submitting}
            className="mt-1 w-full rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white shadow-card transition hover:bg-brand-700 disabled:opacity-50"
          >
            {submitting ? "Resetting..." : "Reset password"}
          </button>
        </form>
      )}
    </AuthLayout>
  );
}
