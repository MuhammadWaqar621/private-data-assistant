import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import AuthLayout from "../components/AuthLayout";
import { ApiError, api } from "../lib/api";
import { setTokens } from "../lib/auth";
import { PASSWORD_POLICY_HINT, passwordPolicyError } from "../lib/passwordPolicy";

type TokenResponse = {
  access_token: string;
  refresh_token: string;
  token_type: string;
};

const inputClass =
  "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm transition focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-700 dark:bg-slate-800 dark:text-white";
const labelClass = "mb-1 block text-sm font-medium text-slate-700 dark:text-slate-300";

export default function SignupPage() {
  const navigate = useNavigate();

  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);

    if (password !== confirmPassword) {
      setError("Passwords don't match.");
      return;
    }
    const policyError = passwordPolicyError(password);
    if (policyError) {
      setError(policyError);
      return;
    }

    setSubmitting(true);
    try {
      const tokens = await api.post<TokenResponse>("/api/auth/signup", {
        email,
        full_name: fullName,
        password,
      });
      setTokens(tokens.access_token, tokens.refresh_token);
      navigate("/app");
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

  return (
    <AuthLayout
      title="Create your account"
      subtitle="Connect a database and start asking it questions."
    >
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <div>
          <label htmlFor="fullName" className={labelClass}>
            Full name
          </label>
          <input
            id="fullName"
            type="text"
            required
            autoComplete="name"
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
            className={inputClass}
          />
        </div>

        <div>
          <label htmlFor="email" className={labelClass}>
            Email
          </label>
          <input
            id="email"
            type="email"
            required
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className={inputClass}
          />
        </div>

        <div>
          <label htmlFor="password" className={labelClass}>
            Password
          </label>
          <input
            id="password"
            type="password"
            required
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={inputClass}
          />
          <p className="mt-1 text-xs text-slate-400 dark:text-slate-500">{PASSWORD_POLICY_HINT}</p>
        </div>

        <div>
          <label htmlFor="confirmPassword" className={labelClass}>
            Confirm password
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
          {submitting ? "Creating account..." : "Sign up"}
        </button>
      </form>

      <p className="mt-5 text-center text-sm text-slate-500 dark:text-slate-400">
        Already have an account?{" "}
        <Link to="/login" className="font-medium text-brand-600 hover:underline">
          Log in
        </Link>
      </p>
    </AuthLayout>
  );
}
